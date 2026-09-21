"""Load the approved run and prepare the MainAgent's context."""

import asyncio
import os

from parser.src.db.runs import load_run_context
from sqlalchemy import create_engine

from agent.prompt import build_system_prompt
from agent.state import State
from config import PipelineConfig
from hardware import inspect_hardware


def load_agent_context(run_id: str) -> dict:
    """Read the stored configuration for an unfinished run."""
    engine = create_engine(os.environ["DATABASE_URL"])

    try:
        return load_run_context(
            engine,
            run_id,
            resume=True,
        )
    finally:
        engine.dispose()


async def prepare_agent(state: State) -> dict:
    """Prepare the prompt before starting pipeline services."""
    if not state.run_id:
        raise ValueError("Create a run with start_run() and supply its run_id")

    run_context = await asyncio.to_thread(
        load_agent_context,
        state.run_id,
    )
    current_hardware = await asyncio.to_thread(inspect_hardware)
    config = PipelineConfig.from_dict(run_context["config"]).snapshot()

    return {
        "config": config,
        "fill_threshold": 1,
        "schema_id": run_context["schema_id"],
        "current_hardware": current_hardware,
        "system_prompt": build_system_prompt(
            {**run_context, "config": config},
            current_hardware,
        ),
    }
