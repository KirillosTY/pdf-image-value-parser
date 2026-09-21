"""Define the main agent's state sections and worker event envelope."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass
class RedisState:
    """Track Redis availability without storing a live client in graph state."""

    status: Literal["idle", "starting", "ready", "failed"] = "idle"
    connection_ref: str | None = None
    managed_by_run: bool = False


@dataclass
class DoclingState:
    """Track the producer; filled still means extraction is running."""

    status: Literal["idle", "running", "filled", "completed", "failed"] = "idle"
    processed_pdfs: int = 0
    queued_pdfs: int = 0
    failed_pdfs: int = 0
    no_assets_pdfs: int = 0
    filled_at: str | None = None
    completed_at: str | None = None
    terminal_pdfs: dict[str, str] = field(default_factory=dict)


@dataclass
class QueueState:
    """Count only this run's outstanding work, not historical stream entries."""

    waiting_pdfs: int = 0
    in_flight_pdfs: int = 0
    in_flight_images: int = 0
    pending_analysis: int = 0
    pending_field_mapping: int = 0
    pending_writes: int = 0


@dataclass
class VlmState:
    """Track separately running VLM instances and their request limits."""

    status: Literal["idle", "running", "draining", "completed", "failed"] = "idle"
    vlm_instances: int = 1
    requests_per_instance: int = 1
    instance_endpoints: dict[str, str] = field(default_factory=dict)
    active_instances: int = 0
    processed_images: int = 0
    failed_images: int = 0


@dataclass
class FormatterState:
    """Track language-model workers that normalize unconstrained VLM output."""

    status: Literal["idle", "running", "draining", "completed", "failed"] = "idle"
    model: str | None = None
    endpoint: str | None = None
    max_workers: int = 1
    active_workers: int = 0
    processed_images: int = 0
    failed_images: int = 0


@dataclass
class ImageState:
    """Track analysis and storage for one document-attempt/asset/image key."""

    result_ref: str | None = None
    vlm_model: str | None = None
    formatter_model: str | None = None
    chart_type: str | None = None
    analysis_status: Literal["pending", "running", "completed", "failed"] = "pending"
    fields_ref: str | None = None
    unmapped_observations_ref: str | None = None
    storage_status: Literal[
        "pending", "mapping", "writing", "stored", "failed"
    ] = "pending"
    record_id: str | None = None
    error: str | None = None
    stage: Literal["queued", "vlm", "analysis", "mapping", "writing", "stored", "failed"] = "queued"
    normalized_ref: str | None = None


@dataclass
class DocumentState:
    """Track each published PDF and its expected image identities."""

    image_keys: list[str] = field(default_factory=list)
    claimed: bool = False


@dataclass
class State:
    """Own the pipeline state; keep images and full data in external storage."""

    input_path: str = ""
    run_id: str | None = None
    status: Literal[
        "starting", "running", "completed", "completed_with_errors", "failed"
    ] = "starting"
    started_at: str | None = None
    completed_at: str | None = None
    config: dict[str, Any] = field(default_factory=dict)
    schema_id: str | None = None
    current_hardware: dict[str, Any] = field(default_factory=dict)
    system_prompt: str | None = None
    VLM_INPUT: dict[str, str] = field(default_factory=dict)
    FORMATTER_INPUT: dict[str, str] = field(default_factory=dict)
    vlm_queues: dict[str, str] = field(default_factory=dict)
    formatter_queues: dict[str, str] = field(default_factory=dict)
    fill_threshold: int = 1
    resource_plan: list[list[str]] = field(default_factory=list)
    graph_managed_stages: bool = False
    active_manifests: list[str] = field(default_factory=list)
    pipeline_done: bool = False
    redis: RedisState = field(default_factory=RedisState)
    docling: DoclingState = field(default_factory=DoclingState)
    queues: QueueState = field(default_factory=QueueState)
    vlm: VlmState = field(default_factory=VlmState)
    formatter: FormatterState = field(default_factory=FormatterState)
    images: dict[str, ImageState] = field(default_factory=dict)
    documents: dict[str, DocumentState] = field(default_factory=dict)
    processed_event_ids: list[str] = field(default_factory=list)
    current_event: WorkerEvent | None = None
    pending_actions: list[str] = field(default_factory=list)
    agent_messages: list[dict[str, Any]] = field(default_factory=list)
    agent_tool_call: dict[str, Any] | None = None
    agent_input_errors: int = 0
    startup_turns: int = 0
    startup_attempts: dict[str, int] = field(default_factory=dict)
    startup_last_error: dict[str, Any] | None = None
    startup_diagnostics: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        """Restore typed sections when input or checkpoints contain dictionaries."""
        for name, cls in (
            ("redis", RedisState), ("docling", DoclingState), ("queues", QueueState),
            ("vlm", VlmState), ("formatter", FormatterState),
        ):
            value = getattr(self, name)
            if isinstance(value, dict):
                setattr(self, name, cls(**value))
        self.images = {key: ImageState(**value) if isinstance(value, dict) else value
                       for key, value in self.images.items()}
        self.documents = {key: DocumentState(**value) if isinstance(value, dict) else value
                          for key, value in self.documents.items()}
        if isinstance(self.current_event, dict):
            self.current_event = WorkerEvent(**self.current_event)


@dataclass
class WorkerEvent:
    """Identify a worker notification and references to its external payload."""

    event_id: str
    run_id: str
    kind: Literal[
        "docling.pdf_queued", "docling.pdf_finished", "docling.filled",
        "docling.completed", "vlm.pdf_claimed",
        "vlm.image_completed", "analysis.completed", "db_fields.completed",
        "writer.committed", "worker.item_failed", "worker.failed",
    ]
    source: str
    timestamp: str
    document_attempt_id: str | None = None
    asset_id: str | None = None
    image_ordinal: int | None = None
    payload_ref: str | None = None
    error: str | None = None
    image_keys: list[str] = field(default_factory=list)
    image_key: str | None = None
    chart_type: str | None = None
    record_id: str | None = None
    unmapped_observations_ref: str | None = None
    outcome: Literal["no_assets", "failed"] | None = None
