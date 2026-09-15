"""Expose the deterministic extraction worker as a callable LangGraph subgraph."""

from __future__ import annotations

from collections import Counter
from typing import Any

from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from parser.src.docling.docling_tool import (
    DEFAULT_IMAGES,
    DocumentWorker,
    ExtractionConfig,
)
from typing_extensions import TypedDict


class ExtractionState(TypedDict, total=False):
    """Pass a local PDF/folder and optional worker configuration."""

    input_path: str
    output_dir: str
    config: dict[str, Any]
    pipeline_config: dict[str, Any]
    recursive: bool
    run_id: str
    database_url: str
    resume: bool
    counts: dict[str, int]


def extract_folder(state: ExtractionState) -> dict[str, Any]:
    """Stream per-document completion events and return a bounded summary."""
    writer = get_stream_writer()
    database_engine = None
    if state.get("database_url"):
        from parser.src.db.runs import require_incomplete_run, start_run
        from sqlalchemy import create_engine

        database_engine = create_engine(state["database_url"])
    worker = DocumentWorker(
        state.get("output_dir", str(DEFAULT_IMAGES)),
        ExtractionConfig(**state.get("config", {})),
        run_id=state.get("run_id"),
    )
    counts: Counter[str] = Counter()
    try:
        if state.get("pipeline_config") is not None and database_engine is None:
            raise ValueError("database_url is required to snapshot pipeline_config")
        if database_engine is not None:
            if state.get("resume"):
                if not state.get("run_id"):
                    raise ValueError("An existing run_id is required for resume")
                require_incomplete_run(database_engine, worker.run_id)
            else:
                from config import PipelineConfig

                configured = state.get("pipeline_config")
                start_run(
                    database_engine,
                    run_id=worker.run_id,
                    config=PipelineConfig.from_dict(configured)
                    if configured is not None
                    else None,
                )
        manifests = worker.process_folder(
            state["input_path"],
            recursive=state.get("recursive", False),
            database_engine=database_engine,
            resume=state.get("resume", False),
        )
        for manifest in manifests:
            event = {
                "status": manifest.status,
                "run_id": worker.run_id,
                "document_id": manifest.document_id,
                "manifest_key": manifest.manifest_key,
            }
            counts[manifest.status] += 1
            writer(event)
        return {"run_id": worker.run_id, "counts": dict(counts)}
    finally:
        if database_engine is not None:
            database_engine.dispose()


graph = (
    StateGraph(ExtractionState)
    .add_node("extract_folder", extract_folder)
    .add_edge(START, "extract_folder")
    .add_edge("extract_folder", END)
    .compile(name="Scientific document extraction")
)
