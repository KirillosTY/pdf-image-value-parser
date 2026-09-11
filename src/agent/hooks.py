"""Name future worker adapters without implementing their tools yet.

Start/schedule hooks should return promptly with serializable state updates.
Workers publish events; only the coordinator applies updates to shared state.
No Redis process, model call, or database write happens on import.
"""

from typing import Any

from agent.state import State, WorkerEvent

StateUpdate = dict[str, Any]


async def ensure_redis(state: State) -> StateUpdate:
    """Connect to Redis, or start a managed server, and verify readiness."""
    # TODO: Initialize queues using the coordinator-assigned run_id/config.
    # Record connection_ref and process ownership; do not save a client in state.
    raise NotImplementedError("ensure_redis: Redis lifecycle adapter is not wired")


async def start_docling(state: State) -> StateUpdate:
    """Launch the background PDF producer after Redis is ready."""
    # TODO: Adapt DocumentWorker to publish run-scoped events after enqueueing.
    # Emit filled at five waiting PDFs; emit completed after the final enqueue.
    # Return immediately, leaving Docling running while the main agent listens.
    raise NotImplementedError("start_docling: background producer is not wired")


async def wait_for_worker_event(state: State) -> WorkerEvent:
    """Wait for a recoverable event from this run's workers."""
    # TODO: Use a blocking/event-driven wait, with cancellation and worker health
    # checks. Persist event IDs for deduplication and recovery across restarts.
    raise NotImplementedError("wait_for_worker_event: event transport is not wired")


async def apply_worker_event(state: State, event: WorkerEvent) -> StateUpdate:
    """Apply the coordinator's validated state transition without side effects."""
    from agent.coordinator import apply_event

    return apply_event(state, event)


async def acknowledge_worker_event(state: State, event: WorkerEvent) -> None:
    """Acknowledge a notification after checkpointing its required actions."""
    # TODO: Acknowledge this run's event delivery idempotently. Event IDs must be
    # stable across redelivery. Do not confuse notifications with PDF job claims.
    raise NotImplementedError("acknowledge_worker_event: event transport is not wired")


async def acknowledge_pdf(state: State, event: WorkerEvent) -> StateUpdate:
    """Release a PDF claim after every image has a durable terminal outcome."""
    # TODO: Use event.document_attempt_id to acknowledge the claimed Redis job.
    # On item failure, persist the terminal error and raw output before returning.
    raise NotImplementedError("acknowledge_pdf: PDF acknowledgment is not wired")


async def start_vlm_pool(state: State) -> StateUpdate:
    """Start the configured VLM instances with a request limit per instance."""
    # TODO: Start only when filled or producer completed with waiting PDFs.
    # Provision vlm_instances independently running models and record their
    # endpoints. Limit each to requests_per_instance concurrent requests.
    # Check available capacity before provisioning; report startup failures.
    # Repeated signals reuse the pool. Instances claim distinct Redis jobs.
    # Persist raw output (it need not be JSON or match a schema), then emit
    # vlm.image_completed with its reference. Never discard the original output.
    raise NotImplementedError("start_vlm_pool: VLM consumers are not wired")


async def start_formatter_pool(state: State) -> StateUpdate:
    """Start workers using the configured formatting language-model endpoint."""
    # TODO: Ensure the model endpoint is ready and launch at most max_workers.
    # These workers may share one loaded text model; they do not call the VLM.
    # Consume a bounded result queue independently of the coordinator. When
    # full, apply backpressure upstream without dropping results or events.
    raise NotImplementedError("start_formatter_pool: model workers are not wired")


async def analyze_vlm_result(state: State, event: WorkerEvent) -> StateUpdate:
    """Enqueue raw VLM output for model-backed chart analysis and normalization."""
    # TODO: Return promptly. A formatter worker loads event.payload_ref and uses
    # the text model to resolve chart type and normalize output to its schema.
    # Validate the model's proposed output with code before analysis.completed.
    # A model response alone is not proof of schema compliance or factual accuracy.
    # Preserve raw/normalized references; never invent missing chart values.
    # Account for every extracted observation as mapped or unmapped. Preserve
    # unmapped_observations with source references and reasons; schema-valid
    # output alone does not show that all observations were retained.
    # On validation failure, use bounded repair attempts with validation errors,
    # then emit worker.item_failed if still invalid or unresolved.
    raise NotImplementedError("analyze_vlm_result: chart analysis is not wired")


async def build_db_fields(state: State, event: WorkerEvent) -> StateUpdate:
    """Schedule chart-specific mapping into a permanent record's fields."""
    # TODO: In the formatter worker, map validated values and provenance into an
    # existing storage schema. Validate required fields/types before emitting
    # db_fields.completed. Keep database writes in the separate writer stage.
    # Carry raw extraction and unmapped_observations references through mapping.
    raise NotImplementedError("build_db_fields: field mapper is not wired")


async def write_chart_record(state: State, event: WorkerEvent) -> StateUpdate:
    """Schedule an idempotent permanent write of the mapped record."""
    # TODO: Commit using the document-attempt/asset/image identity, then emit
    # writer.committed with the permanent record ID. Retain failed work to retry.
    # Permanently preserve raw output and unmapped observations with the record;
    # Redis references alone are not permanent storage of their contents.
    raise NotImplementedError("write_chart_record: database writer is not wired")


async def handle_worker_failure(state: State, event: WorkerEvent) -> StateUpdate:
    """Record an item or stage failure and apply the future retry policy."""
    # TODO: Retry eligible items or record durable terminal errors. A stage-level
    # failure must not be treated as successful pipeline completion.
    raise NotImplementedError("handle_worker_failure: failure policy is not wired")


def pipeline_is_finished(state: State) -> bool:
    """Check producer completion, drained queues, and durable item outcomes."""
    from agent.coordinator import pipeline_is_finished as is_finished

    return is_finished(state)


async def finish_run(state: State) -> StateUpdate:
    """Finalize the run and release only resources owned by it."""
    # TODO: Record completed/completed_with_errors/failed and completion time.
    # Stop owned workers; preserve queued work needed for recovery. Never stop
    # a shared Redis server or discard intermediate data needed by another run.
    raise NotImplementedError("finish_run: finalization is not wired")
