"""Implement the named Docling, routing, VLM, formatter and database nodes."""

from dataclasses import replace

from agent import hooks
from agent.coordinator import initialize
from agent.resources import compatible_groups
from agent.state import State


def initialize_staged_run(state: State) -> dict:
    """Make graph nodes own routing, inference and SQL instead of hidden workers."""
    return {**initialize(state), "graph_managed_stages": True}


def schedule_models(state: State) -> dict:
    """Compute a conservative plan when no MainAgent model is configured."""
    return {"resource_plan": compatible_groups(
        state.config, state.current_hardware,
        set(state.vlm_queues) | set(state.formatter_queues),
    )}


async def docling(state: State) -> dict:
    """Wait for Docling's next bounded document batch and expose its image state."""
    runtime = await hooks.runtime_for(state)
    batch = await runtime.next_manifest_batch(state)
    return {**await runtime.progress(state), "manifest_batch": batch}


def choose_image_route(state: State) -> str:
    """Show the real branch between MainAgent reasoning and configured routing."""
    if not state.manifest_batch:
        return "no_more_images"
    if state.config["vlm_think_sorting"] and len(state.vlm_queues) > 1:
        return "thinking_enabled"
    return "direct"


async def main_agent_route_images(state: State) -> dict:
    """Ask MainAgent to select and save each image's VLM queue using source context."""
    runtime = await hooks.runtime_for(state)
    await runtime.route_batch(state, stage="vlm", thinking=True)
    return await runtime.progress(state)


async def route_images_directly(state: State) -> dict:
    """Apply the configured chart map or single-model route without an LLM call."""
    runtime = await hooks.runtime_for(state)
    await runtime.route_batch(state, stage="vlm", thinking=False)
    return await runtime.progress(state)


async def vlm(state: State) -> dict:
    """Enqueue images and execute VLM requests, retries and model groups here."""
    runtime = await hooks.runtime_for(state)
    await runtime.run_model_stage(state, "vlm")
    return await runtime.progress(replace(state, vlm=replace(state.vlm, status="running")))


def choose_formatter_route(state: State) -> str:
    """Choose the formatter-routing branch independently of VLM thinking."""
    if state.config["formatter_think_sorting"] and len(state.formatter_queues) > 1:
        return "thinking_enabled"
    return "direct"


async def main_agent_route_outputs(state: State) -> dict:
    """Ask MainAgent to select each formatter from saved VLM output and context."""
    runtime = await hooks.runtime_for(state)
    await runtime.route_batch(state, stage="formatter", thinking=True)
    return await runtime.progress(state)


async def route_outputs_directly(state: State) -> dict:
    """Apply the configured formatter map or single-model route without an LLM call."""
    runtime = await hooks.runtime_for(state)
    await runtime.route_batch(state, stage="formatter", thinking=False)
    return await runtime.progress(state)


async def formatter(state: State) -> dict:
    """Execute formatter requests and retries against the approved schema here."""
    runtime = await hooks.runtime_for(state)
    await runtime.run_model_stage(state, "formatter")
    return await runtime.progress(
        replace(state, formatter=replace(state.formatter, status="running")),
    )


async def map_database_fields(state: State) -> dict:
    """Map validated formatter results into the run's approved SQL structure."""
    runtime = await hooks.runtime_for(state)
    await runtime.map_batch(state)
    return await runtime.progress(state)


async def write_database(state: State) -> dict:
    """Commit eligible whole-document batches and flush the final smaller batch."""
    runtime = await hooks.runtime_for(state)
    await runtime.write_documents(state)
    return await runtime.progress(state)


def after_database(state: State) -> str:
    """Read the next Docling batch or finish after the empty final batch is flushed."""
    return "next_batch" if state.manifest_batch else "finished"
