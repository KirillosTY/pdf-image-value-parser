"""Prepare the run's model queues from its routing decisions."""

from parser.src.redis.connection import async_redis_client
from parser.src.redis.queues import create_model_queues

from agent.state import State


async def create_vlm_queues(state: State) -> dict:
    """Create queues for the VLMs selected by the MainAgent."""
    if state.redis.status != "ready":
        raise ValueError("Redis must be ready before creating queues")

    if not state.run_id or not state.VLM_INPUT:
        raise ValueError("A run and VLM_INPUT mapping are required")

    selected = set(state.VLM_INPUT.values())
    if state.config.get("think_sorting"):
        selected = {key for key, model in state.config["models"].items() if "vlm" in model["roles"]}

    async with async_redis_client() as client:
        queues = await create_model_queues(
            client,
            run_id=state.run_id,
            stage="vlm",
            model_keys=selected,
        )

    return {"vlm_queues": queues}


async def create_formatter_queues(state: State) -> dict:
    """Create queues for the formatters selected by the MainAgent."""
    if state.redis.status != "ready":
        raise ValueError("Redis must be ready before creating queues")

    if not state.run_id or not state.FORMATTER_INPUT:
        raise ValueError(
            "A run and FORMATTER_INPUT mapping are required"
        )

    selected = set(state.FORMATTER_INPUT.values())
    if state.config.get("think_sorting"):
        selected = {key for key, model in state.config["models"].items() if "formatter" in model["roles"]}

    async with async_redis_client() as client:
        queues = await create_model_queues(
            client,
            run_id=state.run_id,
            stage="formatter",
            model_keys=selected,
        )

    return {"formatter_queues": queues}
