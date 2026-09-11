from dataclasses import asdict

import pytest

from agent.coordinator import apply_event, initialize, pipeline_is_finished
from agent.state import State, WorkerEvent


def event(kind, **kwargs):
    return WorkerEvent(event_id=kwargs.pop("event_id", kind), run_id="run",
                       kind=kind, source=kind.split(".")[0], timestamp="now", **kwargs)


def apply(state, notification):
    return State(**{**asdict(state), **apply_event(state, notification)})


def running():
    return State(input_path="/pdfs", run_id="run", docling={"status": "running"})


def queued(state, index=0):
    return apply(state, event("docling.pdf_queued", event_id=f"pdf-{index}",
                             document_attempt_id=f"pdf-{index}", image_keys=[f"image-{index}"]))


def test_five_pdf_start_and_duplicate_readiness():
    state = running()
    for index in range(4):
        state = queued(state, index)
    with pytest.raises(ValueError, match="threshold"):
        apply(state, event("docling.filled"))
    state = queued(state, 4)
    state = apply(state, event("docling.filled"))
    assert state.pending_actions == ["start_vlm_pool"]
    assert state.docling.status == "filled"
    state.pending_actions = []  # Successful dispatch.
    state = apply(state, event("docling.filled"))
    assert state.pending_actions == []
    assert state.docling.queued_pdfs == 5


def test_small_batch_drains_through_storage_and_preserves_references():
    state = apply(queued(running()), event("docling.completed"))
    assert state.vlm.status == "draining"
    assert state.pending_actions == ["start_vlm_pool"]
    state.pending_actions = []
    state = apply(state, event("vlm.pdf_claimed", document_attempt_id="pdf-0"))
    identity = {"document_attempt_id": "pdf-0", "image_key": "image-0"}
    state = apply(state, event("vlm.image_completed", payload_ref="raw", **identity))
    assert state.pending_actions == ["start_formatter_pool", "analyze_vlm_result"]
    assert state.vlm.status == "completed"
    assert not pipeline_is_finished(state)
    state.pending_actions = []
    state = apply(state, event("analysis.completed", payload_ref="normalized",
                              chart_type="LINE_CHART", unmapped_observations_ref="unmapped", **identity))
    assert state.pending_actions == ["build_db_fields"]
    state.pending_actions = []
    state = apply(state, event("db_fields.completed", payload_ref="fields", **identity))
    assert state.pending_actions == ["write_chart_record"]
    state.pending_actions = []
    assert not pipeline_is_finished(state)
    state = apply(state, event("writer.committed", record_id="permanent", **identity))
    assert state.pending_actions == ["acknowledge_pdf"]
    state.pending_actions = []
    assert pipeline_is_finished(state)
    assert state.images["image-0"].result_ref == "raw"
    assert state.images["image-0"].unmapped_observations_ref == "unmapped"
    assert state.queues.in_flight_pdfs == 0


def test_empty_run_needs_no_models():
    state = apply(running(), event("docling.pdf_finished", document_attempt_id="empty",
                                   outcome="no_assets"))
    state = apply(state, event("docling.completed"))
    assert pipeline_is_finished(state)
    assert state.pending_actions == []
    assert state.docling.no_assets_pdfs == 1


def test_invalid_event_does_not_mutate_state():
    state = queued(running())
    before = asdict(state)
    with pytest.raises(ValueError, match="stage writing"):
        apply(state, event("writer.committed", document_attempt_id="pdf-0",
                           image_key="image-0", record_id="bad"))
    assert asdict(state) == before
    wrong_run = event("docling.completed")
    wrong_run.run_id = "other"
    with pytest.raises(ValueError, match="another run"):
        apply(state, wrong_run)


def test_item_failure_waits_for_pdf_acknowledgment():
    state = apply(queued(running()), event("docling.completed"))
    state.pending_actions = []
    state = apply(state, event("vlm.pdf_claimed", document_attempt_id="pdf-0"))
    state = apply(state, event("worker.item_failed", document_attempt_id="pdf-0",
                              image_key="image-0", error="Unreadable image"))
    assert state.pending_actions == ["acknowledge_pdf"]
    assert state.vlm.failed_images == 1
    assert not pipeline_is_finished(state)
    state.pending_actions = []
    assert pipeline_is_finished(state)
    assert state.errors == ["Unreadable image"]


def test_configuration_and_nested_state_roundtrip():
    state = State(input_path="/pdfs", vlm={"vlm_instances": 2, "requests_per_instance": 3})
    assert initialize(state)["run_id"]
    assert State(**asdict(state)).vlm.vlm_instances == 2
    state.vlm.vlm_instances = 0
    with pytest.raises(ValueError, match="vlm_instances"):
        initialize(state)
