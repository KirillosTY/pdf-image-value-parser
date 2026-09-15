# Instructions for the configuring LLM

Complete section 1 before section 2. These instructions prepare configuration
for the pipeline; they do not authorize model downloads, service launches, or
database changes. Show the proposed code and schema to the user before writing
their chosen configuration. Once approved, implement that exact configuration.

## 1. Configure hardware, workload, models, and policies

1. Inspect the machine with `uv run python -m hardware` from the project root.
   This only reads system information and prints JSON. Capture its timestamp,
   CPU model/core count, RAM total/available, and each GPU's identity and VRAM
   total/available in `CONFIG.hardware` in `src/config.py`. Unknown measurements
   stay `None`. The automatic probe covers Linux RAM and NVIDIA GPUs; verify
   other accelerators manually. Hardware must be checked again before model
   loading because available memory changes. The scheduler will enforce limits.
2. Ask which chart types matter, whether documents are scientific/business/other,
   their languages, and the desired balance between accuracy and speed. Fill
   `charts_targeted`, `document_types`, `languages`, and
   `accuracy_speed_preference`. Keep `docling_charts` consistent with the actual
   extractor's accepted labels.
3. Research candidates using their official model cards and serving documentation.
   Present recommendations with source URLs. Verify exact model IDs and roles;
   do not copy the illustrative names in architecture/response.md as facts.
   Confirm each formatter accepts the selected VLM's output. A chart-reading
   model is not automatically a text-to-schema formatter.
4. After model approval, fill the `models` registry with stable local keys and
   `ModelConfig` entries. Include roles, model ID, parameter count, precision,
   context window, output token limit, chart strengths, limitations, expected
   input/output, estimated RAM/VRAM, and explanatory context. Estimates should
   describe their assumptions, including image/context and concurrency overhead.
   Unknown quantities stay `None`; parameter count alone does not establish that
   a model fits. Include the MainAgent model's resource costs if selected.
   Store environment-variable names for endpoints/credentials, never secrets.
5. Set `main_agent` to the registry key of the tool-calling orchestrator. Its
   endpoint must support native function tools. An explicit `None` opts into
   deterministic setup/routing in the stage graph and is incompatible with thinking-based routing.
   `vlm_think_sorting` and `formatter_think_sorting` are independent. Validation
   forces a stage's flag to false when that stage has only one model, and uses
   that model as its fallback. This does not disable MainAgent orchestration.
   With multiple models and deterministic routing,
   fill all `chart_matcher` / `format_matcher` entries and select fallback models.
   Map Docling label → VLM registry key, then VLM registry key → formatter key.
6. Agree on `max_image_retries`: this counts requeues after the first attempt.
   `vlm_retry_count` and `format_retry_count` are separate image-level counters.
   The runtime applies these bounds independently and retries pending jobs
   before accepting new work. A formatter retry reuses the saved VLM output.
7. Agree on `approve_with_fails`:
   - `False`: any terminal image failure blocks the document's database write.
   - `"omit"`: store the entire document and all image identities together. A
     failed image has `images.failed = true`, null raw output, and an
     `extraction_details` row with null extracted fields. Successful images keep
     all their data. No invented chart or measurement rows are inserted.
     `db_status` belongs only to the document.

An empty `PipelineConfig()` is intentionally incomplete. The current `CONFIG`
contains the local chart setup and the user-selected installed Qwen3 abliterated
MainAgent. Do not choose models or invent hardware characteristics just to make
validation pass. The dataclasses
document the editable fields, and `CONFIG.snapshot()` validates the model and
policy setup without connecting to any service.

## 2. Define and associate the schema with runs

1. Ask what values users need to query, required versus optional fields, SQL
   types, units, and relationships. Present an example formatter result and the
   corresponding SQL rows. Resolve missing-value behavior explicitly.
2. Propose a bundle under `schemas/<project>/bundle.json`, using
   [schemas/README.md](../schemas/README.md). It contains `formatter_schemas` and
   `sql_tables` together. Shared tables (`run_info`, `schema_versions`,
   `documents`, `images`) retain pipeline identity and provenance. Configure
   extracted values separately. The default bundle preserves the existing
   chart tables; `records-v1` supports custom image-linked records.
3. Set `CONFIG.schema_bundle` to that file, `schema_matcher` to Docling label →
   output schema key, and `fallback_schema` to a key in the bundle. This is
   independent of choosing the formatter model. Verify the actual formatter
   adapter supports the proposed JSON Schema. In particular, the current
   NuExtract adapter rejects `$ref`, schema unions/conditionals, and unsupported
   types. Do not silently simplify a schema and lose values.
4. Show the completed `src/config.py`, JSON bundle, SQL layout, and example
   output to the user for approval. Then save the approved files and validate:

   ```python
   from config import CONFIG
   from parser.src.db.schemas import load_bundle

   bundle = load_bundle(CONFIG.schema_bundle)
   snapshot = CONFIG.snapshot(bundle["formatter_schemas"])
   ```

5. At new-run creation, `start_run(engine, config=CONFIG)` registers the bundle
   in Postgres and saves its `schema_id` plus the configuration snapshot in
   `run_info`, in one transaction. Schema identity hashes the complete bundle.
   Custom extracted tables include that version in their physical names.
6. To recover, use `load_run_context(engine, run_id, resume=True)`. It retrieves
   the saved configuration and schema; it does not reread local configuration.
   It rejects completed runs and detects changed schema definitions. New runs
   may use a new version without changing previous runs' definitions.
7. The VLM continues producing unrestricted observations. The formatting worker
   retrieves the selected schema from the run, validates the output, and records
   its version/key. The writer verifies that version and mapping before writing
   the document transaction. See the runnable API examples in schemas/README.md.

## Runtime settings

- `manifest_minimum=20`, `manifest_capacity=50`: the startup gate and maximum
  active manifest buffer. The minimum cannot exceed capacity; capacity cannot
  exceed 50. A smaller final batch starts as soon as Docling finishes.
- `writer_batch_size=50`: whole-document transaction size, capped at 50.
- `model_batch_size=50`: maximum jobs per model queue before another group runs.
- `recursive=False` and `extraction={}`: input traversal and Docling
  `ExtractionConfig` options.
- `memory_reserve_gib=2`: reserve for extraction and other overhead. Adjust it to
  measured peak usage for the actual workload.
- Per model, `requests_per_model=1` bounds concurrent inference calls;
  `formatter_adapter="chat"` or `"nuextract"` selects the endpoint protocol.
- `serving="external"` uses a separately provisioned endpoint. For local model
  lifecycle management, use `serving="managed"`, `launch_command=[...]`, and
  verified RAM/VRAM estimates. Set `gpu_index` for the intended GPU. Arguments
  are executed without shell expansion.
  Configure the endpoint environment variable to match that process. The
  MainAgent is externally served so it is available during startup routing.

See [MAIN_AGENT.md](../MAIN_AGENT.md) for execution and recovery. These settings
are included in each run's immutable database configuration snapshot.
