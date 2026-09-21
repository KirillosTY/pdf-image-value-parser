"""Implement the named Docling, routing, VLM, formatter and database nodes."""

from dataclasses import replace

from agent import hooks
from agent.coordinator import initialize
from agent.state import State


def initialize_staged_run(state: State) -> dict:
    """Use visible stages with runtime behavior chosen by the saved batch setting."""
    return {**initialize(state), "graph_managed_stages": True}


async def docling(state: State) -> dict:
    """Expose ready manifests, completing extraction first when batching is enabled."""
    runtime = await hooks.runtime_for(state)
    return {"active_manifests": await runtime.active_manifest_keys()}


async def await_ready_work(state: State) -> dict:
    """Yield to live workers, then expose newly published manifests and results."""
    runtime = await hooks.runtime_for(state)
    await runtime.await_ready_work()
    return {"active_manifests": await runtime.active_manifest_keys()}


def choose_image_route(state: State) -> str:
    """Show the real branch between MainAgent reasoning and configured routing."""
    if state.config["think_sorting"] and len(state.vlm_queues) > 1:
        return "thinking_enabled"
    return "direct"


async def main_agent_route_images(state: State) -> dict:
    """Ask MainAgent to select and save each image's VLM queue using source context."""
    runtime = await hooks.runtime_for(state)
    await runtime.route_ready_images(state, stage="vlm", thinking=True)
    return {}


async def route_images_directly(state: State) -> dict:
    """Apply the configured chart map or single-model route without an LLM call."""
    runtime = await hooks.runtime_for(state)
    await runtime.route_ready_images(state, stage="vlm", thinking=False)
    return {}


async def vlm(state: State) -> dict:
    """Publish VLM work and wait for its completion when batching is enabled."""
    runtime = await hooks.runtime_for(state)
    await runtime.enqueue_ready_images(state, "vlm")
    return {"vlm": replace(state.vlm, status="running")}


def choose_formatter_route(state: State) -> str:
    """Use the same thinking setting and MainAgent as image routing."""
    if state.config["think_sorting"] and len(state.formatter_queues) > 1:
        return "thinking_enabled"
    return "direct"


async def main_agent_route_outputs(state: State) -> dict:
    """Ask MainAgent to select each formatter from saved VLM output and context."""
    runtime = await hooks.runtime_for(state)
    await runtime.route_ready_images(state, stage="formatter", thinking=True)
    return {}


async def route_outputs_directly(state: State) -> dict:
    """Apply the configured formatter map or single-model route without an LLM call."""
    runtime = await hooks.runtime_for(state)
    await runtime.route_ready_images(state, stage="formatter", thinking=False)
    return {}


async def formatter(state: State) -> dict:
    """Publish formatter work and wait for its completion when batching is enabled."""
    runtime = await hooks.runtime_for(state)
    await runtime.enqueue_ready_images(state, "formatter")
    return {"formatter": replace(state.formatter, status="running")}


async def map_database_fields(state: State) -> dict:
    """Map validated formatter results into the run's approved SQL structure."""
    runtime = await hooks.runtime_for(state)
    await runtime.map_ready_images(state)
    return {}


async def write_database(state: State) -> dict:
    """Commit each ready manifest or explicitly block it after an image failure."""
    runtime = await hooks.runtime_for(state)
    await runtime.write_documents(state)
    return {**await runtime.progress(state), "pipeline_done": await runtime.is_finished()}


def after_database(state: State) -> str:
    """Wait for more worker progress, or finish after every manifest settles."""
    return "finished" if state.pipeline_done else "more_work"
