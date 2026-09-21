"""Define defaults and validate run settings loaded from configuration/*.toml.

Edit configuration/run.toml and configuration/models.toml for new runs.
Importing this module does not read those files, inspect hardware, load models,
connect to services, or start a run. Saved runs reconstruct their own snapshots.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from typing import Literal

# These describe the coding-assistant setup workflow, not runtime model work.
# This checkout already has a user-approved configuration. For a new setup,
# set SETUP_COMPLETED=False; to revisit it, set RERUN_SETUP=True.
SETUP_COMPLETED = True
RERUN_SETUP = False


def require_completed_setup() -> None:
    """Point new runs to the configuring assistant when setup is outstanding."""
    if not SETUP_COMPLETED or RERUN_SETUP:
        raise ValueError(
            "Ask the coding assistant to follow configuration/README.md. "
            "After saving the agreed setup, set SETUP_COMPLETED=True and "
            "RERUN_SETUP=False in src/config.py."
        )


@dataclass
class ModelConfig:
    """Keep a model's operational settings beside its human-readable context."""

    model_id: str
    roles: list[Literal["main_agent", "vlm", "formatter"]]
    context: str
    input_description: str
    output_description: str
    parameter_count_billions: float | None = None
    precision: str = "unknown"
    estimated_vram_gib: float | None = None
    estimated_ram_gib: float | None = None
    context_window: int | None = None
    max_output_tokens: int | None = None
    chart_types: list[str] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    source_urls: list[str] = field(default_factory=list)
    endpoint_env: str | None = None
    api_key_env: str | None = None
    device: str = "auto"
    serving: Literal["external", "managed"] = "external"
    # Explicit permission/protocol for unloading this model between batch stages.
    lifecycle: Literal["none", "ollama"] = "none"
    launch_command: list[str] = field(default_factory=list)
    formatter_adapter: Literal["chat", "nuextract"] = "chat"
    formatter_response_format: Literal["json_schema", "json_object", "text"] = "text"
    requests_per_model: int = 1
    gpu_index: int = 0


@dataclass
class RedisRecoveryConfig:
    """Scope startup repairs to one existing local Docker Compose Redis service."""

    enabled: bool = False
    compose_file: str = "configuration/test-services.compose.yml"
    project: str = "parser-chart-test"
    service: str = "redis"
    env_file: str = ".env"
    port: int = 16379

    def validate(self) -> None:
        """Reject missing scope and invalid connection targets before a run starts."""
        if type(self.enabled) is not bool:
            raise ValueError("redis_recovery.enabled must be a boolean")
        for name in ("compose_file", "project", "service", "env_file"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip() or value.startswith("-"):
                raise ValueError(f"redis_recovery.{name} must identify the configured service")
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise ValueError("redis_recovery.port must be a valid TCP port")


@dataclass
class PipelineConfig:
    """Validate setup choices before saving an immutable run snapshot."""

    hardware: dict = field(default_factory=dict)
    models: dict[str, ModelConfig] = field(default_factory=dict)
    main_agent: str | None = None
    charts_targeted: list[str] = field(default_factory=list)
    docling_charts: list[str] = field(
        default_factory=lambda: [
            "line_chart",
            "bar_chart",
            "pie_chart",
            "scatter_plot",
            "box_plot",
            "table",
        ]
    )
    document_types: list[str] = field(default_factory=list)
    languages: list[str] = field(default_factory=list)
    accuracy_speed_preference: str = ""
    think_sorting: bool = True
    # Missing fields in old snapshots retain the original streaming behavior.
    batch_processing: bool = False
    # Print concise actions and stage transitions, without model response text.
    show_thinking: bool = False
    startup_max_turns: int = 24
    startup_max_tool_attempts: int = 3
    redis_recovery: RedisRecoveryConfig = field(default_factory=RedisRecoveryConfig)
    chart_matcher: dict[str, str] = field(default_factory=dict)
    format_matcher: dict[str, str] = field(default_factory=dict)
    fallback_vlm: str | None = None
    fallback_formatter: str | None = None
    schema_bundle: str = "schemas/default/bundle.json"
    schema_matcher: dict[str, str] = field(default_factory=dict)
    fallback_schema: str | None = None
    max_image_retries: int = 3
    approve_with_fails: Literal[False, "omit"] = False
    # Legacy batch APIs retain these settings; streaming starts immediately
    # and writes each completed manifest separately.
    manifest_minimum: int = 1
    manifest_capacity: int = 50
    writer_batch_size: int = 1
    model_batch_size: int = 50
    recursive: bool = False
    extraction: dict = field(default_factory=dict)
    memory_reserve_gib: float = 2.0

    @classmethod
    def from_dict(cls, value: dict) -> PipelineConfig:
        """Reconstruct a snapshot without importing executable configuration."""
        values = dict(value)
        # Read old saved runs without retaining two independently editable flags.
        legacy = [values.pop(name) for name in
                  ("vlm_think_sorting", "formatter_think_sorting") if name in values]
        if "think_sorting" not in values and legacy:
            values["think_sorting"] = any(legacy)
        values["models"] = {
            key: ModelConfig(**model) for key, model in values.get("models", {}).items()
        }
        values["redis_recovery"] = RedisRecoveryConfig(**values.get("redis_recovery", {}))
        return cls(**values)

    def validate(self, schema_keys=None) -> None:
        """Reject missing models, dangling routes, and ambiguous failure policy."""
        if self.approve_with_fails is not False and self.approve_with_fails != "omit":
            raise ValueError("approve_with_fails must be false or 'omit'")
        if type(self.max_image_retries) is not int or self.max_image_retries < 0:
            raise ValueError("max_image_retries must be a nonnegative integer")
        for name in (
            "startup_max_turns",
            "startup_max_tool_attempts",
            "manifest_minimum",
            "manifest_capacity",
            "writer_batch_size",
            "model_batch_size",
        ):
            if type(getattr(self, name)) is not int or getattr(self, name) < 1:
                raise ValueError(f"{name} must be a positive integer")
        if not self.manifest_minimum <= self.manifest_capacity <= 50:
            raise ValueError("Require manifest_minimum <= manifest_capacity <= 50")
        if self.writer_batch_size > 50:
            raise ValueError("writer_batch_size cannot exceed 50")
        if (
            isinstance(self.memory_reserve_gib, bool)
            or not isinstance(self.memory_reserve_gib, (int, float))
            or not math.isfinite(self.memory_reserve_gib)
            or self.memory_reserve_gib < 0
        ):
            raise ValueError("memory_reserve_gib must be finite and nonnegative")
        if not self.charts_targeted:
            raise ValueError("Select charts_targeted during setup")
        if type(self.batch_processing) is not bool:
            raise ValueError("batch_processing must be a boolean")
        if type(self.show_thinking) is not bool:
            raise ValueError("show_thinking must be a boolean")
        if type(self.recursive) is not bool:
            raise ValueError("recursive must be a boolean")
        self.redis_recovery.validate()
        for key, model in self.models.items():
            if model.serving not in {"external", "managed"}:
                raise ValueError(f"Invalid serving mode: {key}")
            if model.lifecycle not in {"none", "ollama"}:
                raise ValueError(f"Invalid model lifecycle: {key}")
            if (self.batch_processing and model.serving == "external"
                    and model.lifecycle != "ollama"
                    and (set(model.roles) & {"vlm", "formatter"}
                         or key == self.main_agent)):
                raise ValueError(f"Batch processing requires lifecycle='ollama' for {key}")
            if model.formatter_adapter not in {"chat", "nuextract"}:
                raise ValueError(f"Invalid formatter adapter: {key}")
            if model.formatter_response_format not in {"json_schema", "json_object", "text"}:
                raise ValueError(f"Invalid formatter response format: {key}")
            if (
                type(model.requests_per_model) is not int
                or model.requests_per_model < 1
            ):
                raise ValueError(f"{key}.requests_per_model must be positive")
            if type(model.gpu_index) is not int or model.gpu_index < 0:
                raise ValueError(f"{key}.gpu_index must be nonnegative")
            if model.serving == "managed" and (
                not model.launch_command
                or not all(isinstance(arg, str) and arg for arg in model.launch_command)
            ):
                raise ValueError(f"{key} requires a launch_command argument list")
            if (model.serving == "managed" and "main_agent" in model.roles
                    and not self.batch_processing):
                raise ValueError(
                    "The MainAgent endpoint must be externally served before startup"
                )
            if (
                not key
                or not model.model_id
                or not model.roles
                or not set(model.roles)
                <= {
                    "main_agent",
                    "vlm",
                    "formatter",
                }
            ):
                raise ValueError(f"Invalid model identity or roles: {key}")
            if not all(
                text.strip()
                for text in (
                    model.context,
                    model.input_description,
                    model.output_description,
                )
            ):
                raise ValueError(f"Describe model context, input, and output: {key}")
            for name in (
                "parameter_count_billions",
                "estimated_vram_gib",
                "estimated_ram_gib",
                "context_window",
                "max_output_tokens",
            ):
                value = getattr(model, name)
                if value is not None and (
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(value)
                    or value <= 0
                ):
                    raise ValueError(f"{key}.{name} must be positive or unknown (None)")

        vlms = {key for key, model in self.models.items() if "vlm" in model.roles}
        formatters = {
            key for key, model in self.models.items() if "formatter" in model.roles
        }
        if not vlms or not formatters:
            raise ValueError("Configure at least one VLM and one formatter")
        for mapping, targets in (
            (self.chart_matcher, vlms),
            (self.format_matcher, formatters),
        ):
            if set(mapping.values()) - targets:
                raise ValueError("A routing map references an unavailable model")
        if set(self.format_matcher) - vlms:
            raise ValueError("format_matcher keys must identify configured VLMs")
        for fallback, available in (
            (self.fallback_vlm, vlms),
            (self.fallback_formatter, formatters),
        ):
            if fallback is not None and fallback not in available:
                raise ValueError("A fallback references an unavailable model")
        if type(self.think_sorting) is not bool:
            raise ValueError("think_sorting must be a boolean")
        # A single model is always routed directly, independently for each stage.
        if len(vlms) == 1:
            self.fallback_vlm = next(iter(vlms))
        if len(formatters) == 1:
            self.fallback_formatter = next(iter(formatters))
        if not self.think_sorting and len(vlms) > 1:
            if (
                not self.fallback_vlm
                or set(self.docling_charts) - self.chart_matcher.keys()
            ):
                raise ValueError("Provide chart_matcher coverage and fallback_vlm")
        if not self.think_sorting and len(formatters) > 1:
            if not self.fallback_formatter or vlms - self.format_matcher.keys():
                raise ValueError(
                    "Provide format_matcher coverage and fallback_formatter"
                )
        if self.main_agent is not None and (
            self.main_agent not in self.models
            or "main_agent" not in self.models[self.main_agent].roles
        ):
            raise ValueError(
                "main_agent must identify a model with the main_agent role"
            )
        if self.think_sorting and (len(vlms) > 1 or len(formatters) > 1) and not self.main_agent:
            raise ValueError("Thinking-based routing requires a main_agent model")
        if schema_keys is not None:
            if self.fallback_schema not in schema_keys:
                raise ValueError("Select a fallback_schema from the approved bundle")
            if set(self.schema_matcher.values()) - set(schema_keys):
                raise ValueError("schema_matcher references a missing output schema")

    def snapshot(self, schema_keys=None) -> dict:
        """Return a validated JSON-serializable copy for run_info."""
        self.validate(schema_keys)
        snapshot = asdict(self)
        json.dumps(snapshot, allow_nan=False)
        return snapshot


def load_config(run_path=None) -> PipelineConfig:
    """Load current TOML choices explicitly when creating a new run."""
    from config_files import load_config as read_config

    return read_config(run_path)


def __getattr__(name):
    """Retain lazy CONFIG access for scripts that import the previous public name."""
    if name == "CONFIG":
        config = load_config()
        globals()[name] = config
        return config
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
