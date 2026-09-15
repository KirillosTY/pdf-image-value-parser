"""Describe approved hardware, models, routing, and storage policy for a run.

Follow configuration/README.md before editing CONFIG. Importing this module
does not inspect hardware, load models, connect to services, or start a run.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from typing import Literal


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
    launch_command: list[str] = field(default_factory=list)
    formatter_adapter: Literal["chat", "nuextract"] = "chat"
    requests_per_model: int = 1
    gpu_index: int = 0


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
    vlm_think_sorting: bool = True
    formatter_think_sorting: bool = False
    chart_matcher: dict[str, str] = field(default_factory=dict)
    format_matcher: dict[str, str] = field(default_factory=dict)
    fallback_vlm: str | None = None
    fallback_formatter: str | None = None
    schema_bundle: str = "schemas/default/bundle.json"
    schema_matcher: dict[str, str] = field(default_factory=dict)
    fallback_schema: str | None = None
    max_image_retries: int = 3
    approve_with_fails: Literal[False, "omit"] = False
    manifest_minimum: int = 20
    manifest_capacity: int = 50
    writer_batch_size: int = 50
    model_batch_size: int = 50
    recursive: bool = False
    extraction: dict = field(default_factory=dict)
    memory_reserve_gib: float = 2.0

    @classmethod
    def from_dict(cls, value: dict) -> PipelineConfig:
        """Reconstruct a snapshot without importing executable configuration."""
        values = dict(value)
        values["models"] = {
            key: ModelConfig(**model) for key, model in values.get("models", {}).items()
        }
        return cls(**values)

    def validate(self, schema_keys=None) -> None:
        """Reject missing models, dangling routes, and ambiguous failure policy."""
        if self.approve_with_fails is not False and self.approve_with_fails != "omit":
            raise ValueError("approve_with_fails must be false or 'omit'")
        if type(self.max_image_retries) is not int or self.max_image_retries < 0:
            raise ValueError("max_image_retries must be a nonnegative integer")
        for name in (
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
        for key, model in self.models.items():
            if model.serving not in {"external", "managed"}:
                raise ValueError(f"Invalid serving mode: {key}")
            if model.formatter_adapter not in {"chat", "nuextract"}:
                raise ValueError(f"Invalid formatter adapter: {key}")
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
            if model.serving == "managed" and "main_agent" in model.roles:
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
        if (
            type(self.vlm_think_sorting) is not bool
            or type(self.formatter_think_sorting) is not bool
        ):
            raise ValueError("Thinking settings must be booleans")
        # A single model is always routed directly, independently for each stage.
        if len(vlms) == 1:
            self.vlm_think_sorting = False
            self.fallback_vlm = next(iter(vlms))
        if len(formatters) == 1:
            self.formatter_think_sorting = False
            self.fallback_formatter = next(iter(formatters))
        if not self.vlm_think_sorting and len(vlms) > 1:
            if (
                not self.fallback_vlm
                or set(self.docling_charts) - self.chart_matcher.keys()
            ):
                raise ValueError("Provide chart_matcher coverage and fallback_vlm")
        if not self.formatter_think_sorting and len(formatters) > 1:
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
        if (
            self.vlm_think_sorting or self.formatter_think_sorting
        ) and not self.main_agent:
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


# Local English chart workload; see configuration/additional-models.md.
CONFIG = PipelineConfig(
    hardware={"checked_at": "2026-09-14T11:22:53.718148+00:00", "platform": "Linux-7.0.0-31-generic-x86_64-with-glibc2.43", "cpu": "Intel(R) Core(TM) Ultra 9 185H", "logical_cpu_count": 22, "ram_total_gib": 30.449893951416016, "ram_available_gib": 18.96860122680664, "gpus": [{"id": "GPU-df2198aa-9e4d-bb8b-a84b-02a3a482e2e4", "name": "NVIDIA GeForce RTX 4060 Laptop GPU", "backend": "cuda", "vram_total_gib": 7.99609375, "vram_available_gib": 7.650390625}], "notes": []},
    models={
        "main_agent": ModelConfig(
            model_id="qwen3-abliterated:latest",
            roles=["main_agent"],
            parameter_count_billions=8.2,
            precision="Q4_K_M",
            context_window=40960,
            max_output_tokens=4096,
            endpoint_env="OLLAMA_BASE_URL",
            serving="external",
            requests_per_model=1,
            input_description="Approved run configuration, schema, hardware, pipeline state, and tool results.",
            output_description="One native tool call per orchestration turn; JSON model selections for thinking-based routing.",
            context="User-selected installed qwen3-abliterated:latest (b07c3bcda724). Local Ollama metadata reports Qwen3 architecture, 8.2B parameters, Q4_K_M, 40960 context, and tools/thinking/completion capabilities. The user's qwen3.8-abliterated name refers to this installed model. Select among three VLMs and three formatters using model context, source captions and observations. The added VLMs target under 4 GB RAM each, but peak usage is unverified: never treat download size as a memory measurement. Ollama owns residency; peak RAM/VRAM is unmeasured.",
            limitations=["Server context allocation must fit the run schema and tool history; context_window is model metadata, not a server setting.", "Tool arguments and resource proposals require validation.", "Peak memory and chart-routing quality remain unmeasured."],
        ),
        "chart_reader": ModelConfig(
            precision="Q4_K_M",
            estimated_ram_gib=None,
            estimated_vram_gib=None,
            chart_types=["bar_chart", "line_chart", "pie_chart", "scatter_plot", "box_plot", "heatmap", "multi_panel"],
            endpoint_env="OLLAMA_BASE_URL",
            api_key_env=None,
            device="auto",
            serving="external",
            requests_per_model=1,
            model_id="parser-chart-vlm:7b",
            roles=["vlm"],
            parameter_count_billions=8.29,
            context_window=8192,
            max_output_tokens=4096,
            input_description="One Docling PNG crop and source context; no output schema or answer key.",
            output_description="Unrestricted observations of all panels, visible values, labels, units, and uncertainty.",
            context="Local alias of installed qwen2.5vl:7b (5ced39dfa4ba), with num_ctx=8192. Official model supports image/chart understanding. Installed weights occupy about 6.0 GB on disk; this is not a RAM/VRAM measurement. Peak usage with one crop and this context has not been measured; estimates remain unknown. Ollama controls residency and possible CPU offload; the app's managed-server memory guard does not constrain this external endpoint.",
            limitations=["Numeric accuracy must be measured on the fixture.", "Small labels, unlabeled points, and multi-panel associations can be missed.", "Disk size is not peak memory; concurrent residency of both models is not assumed."],
            source_urls=["https://huggingface.co/Qwen/Qwen2.5-VL-7B-Instruct", "https://ollama.com/library/qwen2.5vl:7b", "https://docs.ollama.com/api/openai-compatibility"],
        ),
        "chart_formatter": ModelConfig(
            precision="Q4_K_M",
            estimated_ram_gib=None,
            estimated_vram_gib=None,
            chart_types=["bar_chart", "line_chart", "pie_chart", "scatter_plot", "box_plot", "heatmap", "multi_panel"],
            endpoint_env="OLLAMA_BASE_URL",
            api_key_env=None,
            device="auto",
            serving="external",
            requests_per_model=1,
            model_id="parser-chart-formatter:7b",
            roles=["formatter"],
            parameter_count_billions=7.62,
            context_window=16384,
            max_output_tokens=8192,
            formatter_adapter="chat",
            input_description="VLM raw text, JSON Schema, source context, and any previous validation error.",
            output_description="JSON envelope with data matching MEASUREMENTS and unmapped_observations strings.",
            context="Local alias of installed qwen2.5:7b (845dbda0ea48), with num_ctx=16384. Text instruction model supports structured/JSON output, so it can consume the VLM's raw text through the generic chat adapter. Installed weights occupy about 4.7 GB on disk. Peak RAM/VRAM with raw observations, schema, and one output is unmeasured; estimates remain unknown. Ollama manages residency. MainAgent orchestration uses the separately configured installed Qwen3 abliterated model.",
            limitations=["Cannot recover values omitted or misread by the VLM.", "Generic chat JSON is validated and may require retries.", "Long outputs can hit the output limit; truncation is treated as failure."],
            source_urls=["https://ollama.com/library/qwen2.5:7b", "https://docs.ollama.com/api/openai-compatibility"],
        ),
        "qwen_small_reader": ModelConfig(
            model_id="parser-qwen3-vl:2b",
            roles=["vlm"],
            parameter_count_billions=2.13,
            precision="Q4_K_M",
            context_window=4096,
            max_output_tokens=2048,
            endpoint_env="OLLAMA_BASE_URL",
            serving="external",
            requests_per_model=1,
            chart_types=["bar_chart", "line_chart", "pie_chart", "scatter_plot", "table"],
            input_description="One PNG chart crop with caption, mentions and nearby text; 4096-token total context.",
            output_description="Free-form chart observations, labels, values, units and uncertainty; up to 2048 output tokens.",
            context="Ollama alias of qwen3-vl:2b with num_ctx=4096. Qwen3-VL supports OCR and visual understanding. Candidate for simple charts and readable labels where a small model is preferred; chart_types are routing suggestions, not measured rankings. Published quantized package is about 1.9 GB on disk. User requires under 4 GB RAM per added VLM; actual peak RAM/VRAM is unverified and remains unknown. One request and a short context reduce memory demand but do not enforce that limit.",
            limitations=["Small labels, dense scatter plots and complex panels may need chart_reader.", "Short context/output limits can truncate large observations.", "Under-4-GB runtime RAM is a target, not a measured or enforced guarantee."],
            source_urls=["https://ollama.com/library/qwen3-vl:2b", "https://huggingface.co/Qwen/Qwen3-VL-2B-Instruct"],
        ),
        "granite_reader": ModelConfig(
            model_id="parser-granite-vision:2b",
            roles=["vlm"],
            parameter_count_billions=2.97,
            precision="Q4_K_M language model; F16 vision projector",
            context_window=4096,
            max_output_tokens=2048,
            endpoint_env="OLLAMA_BASE_URL",
            serving="external",
            requests_per_model=1,
            chart_types=["table", "bar_chart", "line_chart", "heatmap", "multi_panel"],
            input_description="One document/chart PNG crop and source text; 4096-token total context.",
            output_description="Free-form observations of chart/table structure, visible values, labels, units and uncertainty.",
            context="Ollama alias of granite3.2-vision:2b with num_ctx reduced from 16384 to 4096. IBM designed this family for charts, tables, infographics, plots and document understanding. Candidate for tables and document-style figures; quality on this workload is unmeasured. Ollama lists a 2.53B language component plus a 442M projector, approximately 2.97B total, in a roughly 2.4 GB package. User requires under 4 GB RAM per added VLM; disk size is not peak inference memory. Actual RAM/VRAM remains unknown.",
            limitations=["Dense figures and small labels can be misread.", "Short context/output limits can truncate dense tables or multi-panel output.", "Under-4-GB runtime RAM is unverified; Ollama owns memory allocation."],
            source_urls=["https://ollama.com/library/granite3.2-vision:2b", "https://huggingface.co/ibm-granite/granite-vision-3.2-2b"],
        ),
        "qwen_small_formatter": ModelConfig(
            model_id="parser-qwen-formatter:3b",
            roles=["formatter"],
            parameter_count_billions=3.09,
            precision="Q4_K_M",
            context_window=8192,
            max_output_tokens=4096,
            endpoint_env="OLLAMA_BASE_URL",
            serving="external",
            requests_per_model=1,
            formatter_adapter="chat",
            input_description="Saved VLM text, the approved JSON Schema, source context and any validation error.",
            output_description="JSON containing data matching MEASUREMENTS and unmapped_observations; up to 4096 tokens.",
            context="Ollama alias of qwen2.5:3b with num_ctx=8192. Qwen2.5 emphasizes structured-data understanding and JSON generation. Smaller alternative to chart_formatter for short, straightforward observations. Published Q4_K_M package is about 1.9 GB; peak memory is unmeasured. Uses the generic chat adapter, which preserves the schema's nullable measurement fields and validates the full response.",
            limitations=["Cannot recover missing or incorrect VLM readings.", "May omit rows or produce invalid JSON; existing validation and retries still apply.", "Prefer chart_formatter for long outputs or difficult multi-panel associations."],
            source_urls=["https://ollama.com/library/qwen2.5:3b", "https://huggingface.co/Qwen/Qwen2.5-3B-Instruct"],
        ),
        "llama_formatter": ModelConfig(
            model_id="parser-llama-formatter:3b",
            roles=["formatter"],
            parameter_count_billions=3.21,
            precision="Q4_K_M",
            context_window=8192,
            max_output_tokens=4096,
            endpoint_env="OLLAMA_BASE_URL",
            serving="external",
            requests_per_model=1,
            formatter_adapter="chat",
            input_description="Saved VLM observations plus approved schema, source context and previous validation error.",
            output_description="Validated JSON data and unmapped_observations using the existing chat envelope.",
            context="Ollama alias of llama3.2:3b with num_ctx=8192. Meta's small instruction-following text model provides an alternative family for organizing concise English observations. Its usefulness as a measurement formatter is an application choice, not a published task-specific guarantee; it has not been evaluated here. Published Q4_K_M package is about 2.0 GB and actual peak memory is unknown. Use generic chat formatting to retain nullable schema fields.",
            limitations=["Not a specialized numeric or chart extraction model.", "May emit prose or invalid JSON; output must pass the existing validator.", "Cannot correct VLM mistakes; long or complex outputs may need chart_formatter."],
            source_urls=["https://ollama.com/library/llama3.2:3b"],
        ),
    },
    main_agent="main_agent",
    charts_targeted=["bar_chart", "line_chart", "pie_chart", "scatter_plot", "box_plot", "heatmap", "multi_panel"],
    docling_charts=["line_chart", "bar_chart", "pie_chart", "scatter_plot", "box_plot", "table"],
    document_types=["scientific", "business"],
    languages=["English"],
    accuracy_speed_preference="accuracy-first; local quantized baseline using the English test PDF",
    vlm_think_sorting=True,
    formatter_think_sorting=True,
    chart_matcher={"line_chart": "chart_reader", "bar_chart": "chart_reader", "pie_chart": "chart_reader", "scatter_plot": "chart_reader", "box_plot": "chart_reader", "table": "chart_reader"},
    format_matcher={
        "chart_reader": "chart_formatter",
        "qwen_small_reader": "qwen_small_formatter",
        "granite_reader": "llama_formatter",
    },
    fallback_vlm="chart_reader",
    fallback_formatter="chart_formatter",
    schema_bundle="schemas/test-pdf/bundle.json",
    schema_matcher={"line_chart": "MEASUREMENTS", "bar_chart": "MEASUREMENTS", "pie_chart": "MEASUREMENTS", "scatter_plot": "MEASUREMENTS", "box_plot": "MEASUREMENTS", "table": "MEASUREMENTS"},
    fallback_schema="MEASUREMENTS",
    max_image_retries=3,
    approve_with_fails=False,
    manifest_minimum=1,
    manifest_capacity=50,
    writer_batch_size=1,
    model_batch_size=50,
    recursive=False,
    extraction={},
    memory_reserve_gib=2,
)
