"""Expose the deterministic extraction worker as a callable LangGraph subgraph."""

from __future__ import annotations

from collections import Counter
from typing import Any

from parser.src.docling.docling_tool import DEFAULT_IMAGES, DocumentWorker, ExtractionConfig
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from typing_extensions import TypedDict


class ExtractionState(TypedDict, total=False):
    """Pass a local PDF/folder and optional worker configuration."""

    input_path: str
    output_dir: str
    config: dict[str, Any]
    recursive: bool
    run_id: str
    counts: dict[str, int]


def extract_folder(state: ExtractionState) -> dict[str, Any]:
    """Stream per-document completion events and return a bounded summary."""
    writer = get_stream_writer()
    worker = DocumentWorker(
        state.get("output_dir", str(DEFAULT_IMAGES)),
        ExtractionConfig(**state.get("config", {})),
        on_document=writer,
    )
    counts: Counter[str] = Counter()
    for event in worker.iter_folder(
        state["input_path"],
        recursive=state.get("recursive", False),
    ):
        counts[event["status"]] += 1
    return {
        "run_id": worker.run_id,
        "counts": dict(counts),
        "output_dir": str(worker.output_dir),
    }


graph = (
    StateGraph(ExtractionState)
    .add_node("extract_folder", extract_folder)
    .add_edge(START, "extract_folder")
    .add_edge("extract_folder", END)
    .compile(name="Scientific document extraction")
)
