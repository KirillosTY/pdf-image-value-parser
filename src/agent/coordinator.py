"""Apply worker events and plan side effects without running inference."""

from copy import deepcopy
from dataclasses import fields
from datetime import datetime, timezone
from uuid import uuid4

from agent.state import DocumentState, ImageState, State, WorkerEvent


def state_update(state: State) -> dict:
    """Return graph updates while preserving typed nested sections."""
    return {item.name: getattr(state, item.name) for item in fields(state)}


def initialize(state: State) -> dict:
    """Validate run configuration and allocate the run identity."""
    if not state.input_path:
        raise ValueError("input_path is required")
    for name, value in (
        ("fill_threshold", state.fill_threshold),
        ("vlm_instances", state.vlm.vlm_instances),
        ("requests_per_instance", state.vlm.requests_per_instance),
        ("formatter.max_workers", state.formatter.max_workers),
    ):
        if type(value) is not int or value < 1:
            raise ValueError(f"{name} must be a positive integer")
    return {
        "run_id": state.run_id or uuid4().hex,
        "started_at": state.started_at or datetime.now(timezone.utc).isoformat(),
    }


def refresh_progress(state: State) -> None:
    """Derive outstanding-work counters from tracked documents and images."""
    stages = [image.stage for image in state.images.values()]
    state.queues.waiting_pdfs = sum(not doc.claimed for doc in state.documents.values())
    state.queues.in_flight_pdfs = sum(
        doc.claimed and any(state.images[key].stage not in {"stored", "failed"}
                            for key in doc.image_keys)
        for doc in state.documents.values()
    )
    state.queues.in_flight_images = stages.count("vlm")
    state.queues.pending_analysis = stages.count("analysis")
    state.queues.pending_field_mapping = stages.count("mapping")
    state.queues.pending_writes = stages.count("writing")
    if state.docling.status == "completed" and state.vlm.status != "failed":
        state.vlm.status = "draining" if any(
            stage in {"queued", "vlm"} for stage in stages
        ) else "completed"
    if state.vlm.status == "completed" and state.formatter.status != "failed":
        state.formatter.status = "draining" if any(
            stage in {"analysis", "mapping"} for stage in stages
        ) else "completed"


def pipeline_is_finished(state: State) -> bool:
    """Require producer completion and terminal outcomes for every known image."""
    return state.docling.status == "completed" and all(
        image.stage in {"stored", "failed"} for image in state.images.values()
    ) and not state.pending_actions


# --------------------------------------------------------------------------- #
# Per-event-kind handlers
#
# Each handler mutates the already-deepcopied ``state`` in place and appends any
# follow-up work to ``state.pending_actions``. ``apply_event`` picks one by kind.
# --------------------------------------------------------------------------- #

def _maybe_start_vlm_pool(state: State) -> None:
    """Schedule the VLM pool once PDFs are waiting and none is running yet."""
    if state.vlm.status == "idle" and state.queues.waiting_pdfs:
        state.pending_actions.append("start_vlm_pool")
        state.vlm.status = "running"


def _docling_pdf_queued(state: State, event: WorkerEvent) -> None:
    """Register a newly produced PDF and its distinct image crops."""
    if state.docling.status not in {"running", "filled"}:
        raise ValueError("PDF publication requires an active producer")
    document_id = event.document_attempt_id
    if not document_id or document_id in state.docling.terminal_pdfs:
        raise ValueError("PDF publication requires a new document attempt")
    if not event.image_keys or len(set(event.image_keys)) != len(event.image_keys):
        raise ValueError("PDF publication requires distinct image keys")
    if any(key in state.images for key in event.image_keys):
        raise ValueError("Image keys must be unique across the run")
    state.documents[document_id] = DocumentState(list(event.image_keys))
    state.images.update({key: ImageState() for key in event.image_keys})
    state.docling.queued_pdfs += 1
    state.docling.processed_pdfs += 1
    state.docling.terminal_pdfs[document_id] = "queued"


def _docling_pdf_finished(state: State, event: WorkerEvent) -> None:
    """Record a PDF that produced no assets or failed before any crop."""
    document_id = event.document_attempt_id
    if state.docling.status not in {"running", "filled"}:
        raise ValueError("PDF completion requires an active producer")
    if not document_id or document_id in state.docling.terminal_pdfs:
        raise ValueError("PDF completion requires a new document attempt")
    if event.outcome not in {"no_assets", "failed"}:
        raise ValueError("Unqueued PDF must have a no_assets or failed outcome")
    state.docling.terminal_pdfs[document_id] = event.outcome
    state.docling.processed_pdfs += 1
    if event.outcome == "no_assets":
        state.docling.no_assets_pdfs += 1
    else:
        state.docling.failed_pdfs += 1
        state.errors.append(event.error or f"Docling failed: {document_id}")


def _docling_filled(state: State, event: WorkerEvent) -> None:
    """Mark the startup threshold reached and start VLM if work is waiting."""
    if state.docling.status not in {"running", "filled"}:
        raise ValueError("filled requires an active producer")
    if state.docling.filled_at is None:
        if state.queues.waiting_pdfs < state.fill_threshold:
            raise ValueError("filled received before the PDF threshold")
        state.docling.filled_at = event.timestamp
        state.docling.status = "filled"
    _maybe_start_vlm_pool(state)


def _docling_completed(state: State, event: WorkerEvent) -> None:
    """Mark the producer finished and start VLM for any waiting remainder."""
    if state.docling.status not in {"running", "filled", "completed"}:
        raise ValueError("completed requires an active producer")
    state.docling.status = "completed"
    state.docling.completed_at = event.timestamp
    _maybe_start_vlm_pool(state)


def _vlm_pdf_claimed(state: State, event: WorkerEvent) -> None:
    """Move a claimed PDF's images into VLM processing."""
    doc = state.documents.get(event.document_attempt_id)
    if doc is None or doc.claimed or state.vlm.status not in {"running", "draining"}:
        raise ValueError("Invalid PDF claim")
    doc.claimed = True
    for key in doc.image_keys:
        state.images[key].stage = "vlm"


def _worker_failed(state: State, event: WorkerEvent) -> None:
    """Record a fatal worker failure for the whole run."""
    state.status = "failed"
    state.errors.append(event.error or f"{event.source} failed")
    if event.source in {"docling", "vlm", "formatter", "redis"}:
        getattr(state, event.source).status = "failed"


#: Image stage transitions: kind -> (required stage, next stage, follow-up action).
_TRANSITIONS = {
    "vlm.image_completed": ("vlm", "analysis", "analyze_vlm_result"),
    "analysis.completed": ("analysis", "mapping", "build_db_fields"),
    "db_fields.completed": ("mapping", "writing", "write_chart_record"),
    "writer.committed": ("writing", "stored", None),
}


def _item_failed(state: State, event: WorkerEvent, image: ImageState) -> None:
    """Record a per-image failure at its current active stage."""
    if image.stage in {"stored", "failed", "queued"}:
        raise ValueError("Cannot fail an inactive or terminal image")
    if image.stage == "vlm":
        state.vlm.failed_images += 1
    else:
        state.formatter.failed_images += 1
    image.stage = "failed"
    image.error = event.error or f"{event.source} failed"
    image.storage_status = "failed"
    image.analysis_status = "failed" if image.analysis_status != "completed" else "completed"
    state.errors.append(image.error)


def _stage_transition(state: State, event: WorkerEvent, image: ImageState) -> None:
    """Advance one image to its next stage and schedule the follow-up action."""
    kind = event.kind
    if kind not in _TRANSITIONS:
        raise ValueError(f"Unsupported worker event: {kind}")
    expected, next_stage, action = _TRANSITIONS[kind]
    if image.stage != expected:
        raise ValueError(f"{kind} requires image stage {expected}")
    if kind != "writer.committed" and not event.payload_ref:
        raise ValueError(f"{kind} requires payload_ref")
    if kind == "vlm.image_completed":
        image.result_ref = event.payload_ref
        image.analysis_status = "running"
        state.vlm.processed_images += 1
        if state.formatter.status == "idle":
            state.pending_actions.append("start_formatter_pool")
            state.formatter.status = "running"
    elif kind == "analysis.completed":
        if not event.chart_type or not event.unmapped_observations_ref:
            raise ValueError("Analysis requires chart type and unmapped observations reference")
        image.chart_type = event.chart_type
        image.normalized_ref = event.payload_ref
        image.unmapped_observations_ref = event.unmapped_observations_ref
        image.analysis_status = "completed"
        image.storage_status = "mapping"
    elif kind == "db_fields.completed":
        image.fields_ref = event.payload_ref
        image.storage_status = "writing"
        state.formatter.processed_images += 1
    else:
        if not event.record_id:
            raise ValueError("Writer commit requires a permanent record ID")
        image.record_id = event.record_id
        image.storage_status = "stored"
    image.stage = next_stage
    if action:
        state.pending_actions.append(action)


def _image_event(state: State, event: WorkerEvent) -> None:
    """Handle a per-image event, then acknowledge its PDF once all images end."""
    image = state.images.get(event.image_key)
    doc = state.documents.get(event.document_attempt_id)
    if image is None or doc is None or event.image_key not in doc.image_keys:
        raise ValueError("Image event requires a known image and document attempt")
    if event.kind == "worker.item_failed":
        _item_failed(state, event, image)
    else:
        _stage_transition(state, event, image)
    if all(state.images[key].stage in {"stored", "failed"} for key in doc.image_keys):
        state.pending_actions.append("acknowledge_pdf")


#: Event kind -> handler. Image events (item_failed and the stage transitions)
#: are not listed; they fall through to ``_image_event``.
_HANDLERS = {
    "docling.pdf_queued": _docling_pdf_queued,
    "docling.pdf_finished": _docling_pdf_finished,
    "docling.filled": _docling_filled,
    "docling.completed": _docling_completed,
    "vlm.pdf_claimed": _vlm_pdf_claimed,
    "worker.failed": _worker_failed,
}


def apply_event(original: State, event: WorkerEvent) -> dict:
    """Validate an event atomically and persist the actions it requires."""
    if event.run_id != original.run_id:
        raise ValueError("Worker event belongs to another run")
    if not event.event_id:
        raise ValueError("Worker event requires event_id")
    if original.pending_actions:
        raise ValueError("Dispatch pending actions before accepting another event")
    if event.event_id in original.processed_event_ids:
        return {"current_event": event, "pending_actions": []}
    state = deepcopy(original)
    state.current_event = event
    _HANDLERS.get(event.kind, _image_event)(state, event)
    state.processed_event_ids.append(event.event_id)
    refresh_progress(state)
    return state_update(state)
