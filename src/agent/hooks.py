"""Connect checkpointed graph actions to the run's background workers."""

from typing import Any

from parser.src.redis.connection import async_redis_client

from agent.state import RedisState, State, WorkerEvent

StateUpdate = dict[str, Any]


async def runtime_for(state):
    """Import the runtime lazily so graph inspection does not start services."""
    from agent.runtime import get_runtime

    return await get_runtime(state)


async def ensure_redis(state: State) -> StateUpdate:
    """Verify the configured shared Redis server is reachable."""
    async with async_redis_client() as client:
        await client.ping()
    return {
        "redis": RedisState(
            status="ready", connection_ref="REDIS_URL", managed_by_run=False
        )
    }


async def start_docling(state: State) -> StateUpdate:
    """Launch or recover the producer and worker supervisors."""
    runtime = await runtime_for(state)
    return {"resource_plan": runtime.state.resource_plan}


async def wait_for_worker_event(state: State) -> WorkerEvent:
    """Wait for the oldest unacknowledged event without blocking the event loop."""
    return await (await runtime_for(state)).next_event()


async def apply_worker_event(state: State, event: WorkerEvent) -> StateUpdate:
    """Apply a validated event without performing worker side effects."""
    from agent.coordinator import apply_event

    return apply_event(state, event)


async def acknowledge_worker_event(state: State, event: WorkerEvent) -> None:
    """Release a delivery after the graph has applied its actions."""
    await (await runtime_for(state)).acknowledge_event(event)


async def acknowledge_pdf(state: State, event: WorkerEvent) -> StateUpdate:
    """Record a terminal PDF claim while preserving its manifest and errors."""
    await (await runtime_for(state)).acknowledge_pdf(event)
    return {}


async def start_vlm_pool(state: State) -> StateUpdate:
    """Release the startup gate; reuse the run's existing model supervisor."""
    await (await runtime_for(state)).enable_models()
    return {}


async def start_formatter_pool(state: State) -> StateUpdate:
    """Reuse the shared resource scheduler for the formatter model queues."""
    await runtime_for(state)
    return {}


async def analyze_vlm_result(state: State, event: WorkerEvent) -> StateUpdate:
    """Route the saved extraction to its selected formatter."""
    await (await runtime_for(state)).queue_format(event)
    return {}


async def build_db_fields(state: State, event: WorkerEvent) -> StateUpdate:
    """Validate relational mapping before publishing the mapping event."""
    await (await runtime_for(state)).map_fields(event)
    return {}


async def write_chart_record(state: State, event: WorkerEvent) -> StateUpdate:
    """Make the mapped image available to the whole-document batch writer."""
    await (await runtime_for(state)).queue_write(event)
    return {}


async def handle_worker_failure(state: State, event: WorkerEvent) -> StateUpdate:
    """Keep terminal errors in Redis; workers have already exhausted retries."""
    await (await runtime_for(state)).acknowledge_pdf(event)
    return {}


def pipeline_is_finished(state: State) -> bool:
    """Require producer completion and terminal image outcomes."""
    from agent.coordinator import pipeline_is_finished as is_finished

    return is_finished(state)


async def finish_run(state: State) -> StateUpdate:
    """Drain omitted failures, persist the outcome, and release owned resources."""
    runtime = await runtime_for(state)
    try:
        if state.status != "failed":
            await runtime.finish_writes()
        outcome = (
            "failed"
            if state.status == "failed"
            else ("completed_with_errors" if state.errors else "completed")
        )
        await runtime.record_outcome(outcome, state.errors)
    finally:
        await runtime.close()
    return {}
