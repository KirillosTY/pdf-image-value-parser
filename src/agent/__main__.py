"""Create an approved run and execute its full document pipeline."""

import argparse
import asyncio
import os

from dotenv import load_dotenv
from parser.src.db.runs import start_run
from sqlalchemy import create_engine

from agent.graph import build_graph
from agent.runtime import _RUNTIMES
from config import load_config, require_completed_setup


async def run_pipeline(input_path: str, *, run_id: str | None = None,
                       config_path: str | None = None) -> dict:
    """Execute a run; always release this invocation's owned background workers."""
    if run_id is not None and config_path is not None:
        raise ValueError("Existing runs use their saved configuration; --config requires a new run")
    if run_id is None:
        require_completed_setup()
        config = load_config(config_path)
        engine = create_engine(os.environ["DATABASE_URL"])
        try:
            run_id = await asyncio.to_thread(start_run, engine, config=config)
        finally:
            engine.dispose()
    try:
        return await build_graph().ainvoke(
            {"run_id": run_id, "input_path": input_path}, {"recursion_limit": 1_000_000}
        )
    finally:
        runtime = _RUNTIMES.pop((asyncio.get_running_loop(), run_id), None)
        if runtime is not None:
            await runtime.close()


def main():
    """Load environment settings and run the configured pipeline."""
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input_path")
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--run-id", help="An approved, unstarted run ID; uses its saved settings")
    selection.add_argument("--config", help="Run TOML file for a new run (default: configuration/run.toml)")
    args = parser.parse_args()
    result = asyncio.run(run_pipeline(args.input_path, run_id=args.run_id, config_path=args.config))
    print(f"{result['run_id']}: {result['status']}")  # noqa: T201


if __name__ == "__main__":
    main()
