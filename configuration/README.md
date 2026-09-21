# Instructions for the configuring coding assistant

## When to run setup

This is a conversation with the coding assistant, not an interactive CLI or
Studio step. Read the module-level flags in `src/config.py` first:

- `SETUP_COMPLETED=False`: run setup for the first time.
- `RERUN_SETUP=True`: revisit setup using the current choices as starting values.
- Otherwise: reuse the configuration; do not repeat the setup questions.

This checkout already contains agreed model/workload/schema choices, so its
completion flag is true. A new, unconfigured project starts with it false.
After saving the agreed choices, set `SETUP_COMPLETED=True` and
`RERUN_SETUP=False`. The CLI refuses to create a new run while setup is pending
and points the user here; it never conducts the interview itself. Existing runs
continue using their saved configuration.

## Where user settings live

- Edit [run.toml](run.toml) for run switches, workload, schema selection and retry policies.
- Edit [models.toml](models.toml) for the model registry, MainAgent selection,
  routing maps, memory reserve and saved hardware observations.
- Keep endpoint URLs and credentials in `.env`; TOML model definitions reference
  their environment-variable names.
- Keep `SETUP_COMPLETED` and `RERUN_SETUP` in `src/config.py` as instructed above.
  That module owns defaults and validation, while `src/config_files.py` reads TOML.

TOML uses lowercase `true`/`false` and has no `null`. Omit optional unknown model
estimates; use `main_agent = ""` to opt out of MainAgent and set
`think_sorting = false` when multiple model candidates remain. A missing MainAgent
selection also defaults to no agent. Unknown or misplaced fields are errors.
Keep top-level switches above the first `[table]` heading: TOML keys below a
heading belong to that table until the next heading.

`show_thinking = true` enables concise console progress: MainAgent startup
decisions, tool outcomes, image routing choices, batch stage changes, and the
final outcome. VLM/formatter responses and internal reasoning are not printed.
Set it to `false` to silence these messages; it does not affect `think_sorting`
or inference. It is saved with the run; older snapshots without it stay quiet.

`load_config()` reads these files only for new runs. `--config PATH` selects an
alternate run file; `models_file` defaults to its sibling `models.toml` and is
resolved relative to the run file. Other project-relative paths retain their
existing behavior; launch from the project root. Existing runs always use their
stored configuration and schema, even if local TOML files are missing or invalid.
The legacy `from config import CONFIG` loads the default profile lazily once;
new code should call `load_config()` to read fresh edits.


Complete section 1 before section 2. These instructions prepare configuration
for the pipeline; they do not authorize model downloads, service launches, or
database changes. Show the proposed code and schema to the user before writing
their chosen configuration. Once approved, implement that exact configuration.

## 1. Configure hardware, workload, models, and policies

1. Inspect the machine with `uv run python -m hardware` from the project root.
   This only reads system information and prints JSON. Capture its timestamp,
   CPU model/core count, RAM total/available, and each GPU's identity and VRAM
   total/available in `[hardware]` in `models.toml`. Omit unknown measurements
   rather than inventing them. The automatic probe covers Linux RAM and NVIDIA GPUs; verify
   other accelerators manually. Hardware must be checked again before model
   loading because available memory changes. Managed models require verified
   estimates; external models with unknown estimates have no guaranteed fit.
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
4. After model approval, fill the `[models.<key>]` tables in `models.toml` with
   stable local keys. These become `ModelConfig` entries. Include roles, model ID, parameter count, precision,
   context window, output token limit, chart strengths, limitations, expected
   input/output, estimated RAM/VRAM, and explanatory context. Estimates should
   describe their assumptions, including image/context and concurrency overhead.
   Omit unknown quantities so they retain their `None` defaults; parameter count alone does not establish that
   a model fits. Include the MainAgent model's resource costs if selected.
   Store environment-variable names for endpoints/credentials, never secrets.
   When available RAM/VRAM cannot support overlapping stages, require
   `batch_processing=True` as described in
   [Low-memory batch processing](#low-memory-batch-processing). The user has
   requested this mode for the existing local setup, where it is enabled; retain
   the agreed models and schema without repeating their selection questions.
5. Set `main_agent` in `models.toml` to the registry key of the tool-calling orchestrator. Its
   endpoint must support native function tools. An empty string (`""`, loaded as `None`) opts into
   deterministic setup/routing in the stage graph and is incompatible with thinking-based routing.
   A configured MainAgent selects startup tools even when `think_sorting=False`.
   `think_sorting` is the single switch for both routing decisions. One
   MainAgent selects both image → VLM and VLM output → formatter queues, with
   at most one decision in flight. A stage with only one model routes directly
   without disabling thinking for the other stage.
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

An empty `PipelineConfig()` is intentionally incomplete. The current TOML files
contain the local chart setup and the user-selected installed Qwen3 abliterated
MainAgent. Do not choose models or invent hardware characteristics just to make
validation pass. The dataclasses
document the supported fields, and `load_config().snapshot()` validates the model and
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
3. Set `schema_bundle` in `run.toml` to that file, `schema_matcher` to Docling label →
   output schema key, and `fallback_schema` to a key in the bundle. This is
   independent of choosing the formatter model. Verify the actual formatter
   adapter supports the proposed JSON Schema. In particular, the current
   NuExtract adapter rejects `$ref`, schema unions/conditionals, and unsupported
   types. Do not silently simplify a schema and lose values.
4. Show the completed `run.toml`, `models.toml`, JSON bundle, SQL layout, and example
   output to the user for approval. Then save the approved files and validate:

   ```python
   from config import load_config
   from parser.src.db.schemas import load_bundle

   config = load_config()
   bundle = load_bundle(config.schema_bundle)
   snapshot = config.snapshot(bundle["formatter_schemas"])
   ```

5. At new-run creation, `start_run(engine, config=config)` registers the bundle
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

## MainAgent startup and recovery

`prepare_agent` loads the saved run and `initialize` validates its basic state.
These bootstrap steps prepare the model's context. Then the configured MainAgent
chooses startup tools and receives their actual results. `think_sorting=False`
fixes model-routing choices but does not disable startup orchestration.

- `startup_max_turns=24`: maximum startup decisions, including diagnosis and retries.
- `startup_max_tool_attempts=3`: maximum attempts per startup tool. Invalid calls
  also count; three consecutive invalid responses terminate startup.
- `redis_recovery`: a `RedisRecoveryConfig` saved in the run snapshot. It is
  disabled by default and for older snapshots. This checkout enables it for
  the existing `parser-chart-test` Compose project's `redis` service in
  `configuration/test-services.compose.yml`, using `.env` and local port 16379.

MainAgent can check Redis connectivity, inspect the configured service status
and recent logs, refresh resource measurements, retry an operation, or stop
with a concrete explanation. Errors and bounded diagnostic output are returned
as tool results with credentials redacted. Failed startup is recorded without
depending on Redis queues; Postgres and the MainAgent endpoint must already work.

The Redis repair tool uses
[`docker compose start`](https://docs.docker.com/reference/cli/docker/compose/start/)
on the configured existing stopped service and verifies the connection afterward.
The configured Redis URL must target the approved local port. The tool cannot
install or create containers, change credentials, restart running services,
delete data, or accept arbitrary model-generated commands. For a different
deployment, configure its recovery scope explicitly or leave recovery disabled.
Provisioning Postgres/Redis and downloading models remain separate setup actions.

In batch mode MainAgent unloads after each startup decision, before the selected
tool executes. After successful startup, the normal Docling/VLM/formatter batch
sequence begins. Keep tests and live launches paused unless the user resumes them.

## Low-memory batch processing

Set `batch_processing=True` for RAM-limited machines. It is already enabled in
this checkout. `agent.batch_runtime.BatchRuntime` enforces the following stage
barriers; `batch_processing=False` selects the existing streaming runtime.

1. Extract a bounded batch with Docling and persist its manifests and crops.
   Stop extraction and release its model memory before starting routing or
   inference. Do not retain extraction models while downstream models run.
2. If `think_sorting=True` and there is more than one VLM candidate, load
   MainAgent, assign all images in the batch to VLM queues, persist those
   decisions, then unload MainAgent. Otherwise route directly using the saved
   maps or the sole candidate, without a MainAgent call.
3. Process the selected VLM queues one model at a time. Finish that model's
   assigned batch work and bounded retries, save the raw observations, then
   unload it before loading the next VLM. Skip empty queues. This does not send
   every image through every configured VLM.
4. After the VLM stage finishes, load MainAgent only if thinking-based formatter
   selection is needed. Route the saved observations to formatter queues and
   unload MainAgent again. Otherwise use deterministic or single-model routing.
5. Run assigned formatters one at a time, saving and validating results and
   unloading each model before the next. Formatter retries reuse the saved VLM
   output. Write complete manifests under the existing failure policy, then
   begin the next extraction batch.

Keep one active inference stage across the entire pipeline, including startup,
MainAgent calls, extraction, workers, and retries. In this mode, startup must
not keep MainAgent resident while starting Docling or launch continuous
VLM/formatter consumers. Startup MainAgent calls have their own isolated turns;
per-image thinking decisions happen after extraction.
The runtime makes one downstream inference request at a time, regardless of
`requests_per_model`, and persists intermediate work outside model memory.
Docling's internal extraction models run together within its isolated process;
that process exits before any MainAgent, VLM, or formatter is loaded. Extraction
batches contain at most `manifest_capacity` PDFs, including a partial final
batch. Recovery finishes saved unfinished manifests before extracting more.

For managed models, the runtime terminates the previous owned model process
and waits for it to exit. For external models, set `lifecycle="ollama"` only on
an Ollama endpoint that this run may use exclusively. The server stays running;
the runtime uses [`keep_alive=0`](https://docs.ollama.com/api/generate) to unload
configured resident models and checks [`/api/ps`](https://docs.ollama.com/api/ps)
before advancing. Unsupported external lifecycle protocols are rejected during
configuration validation. Unrelated resident models cause an error and are
left untouched. Endpoint leases prevent two batch runs sharing an endpoint;
other applications and streaming runs must not use that endpoint concurrently.

The runtime rechecks available RAM/VRAM before loading each model, allowing for
`memory_reserve_gib`, context, images, and serving overhead. Batch processing
reduces concurrent memory demand; it cannot make a model that is too large
individually fit. Managed estimates are required; known external estimates are
checked too. Unknown external estimates remain unknown, so successful memory
checks establish only the available reserve, not a guaranteed model fit.

`batch_processing` and model lifecycle settings are validated and saved in each
new run's immutable configuration snapshot. Existing runs continue using their
saved settings; missing `batch_processing` means streaming. Start a new run to
use the enabled batch mode. Respect the current pause on tests and live runs
unless the user authorizes resuming them; this mode is not yet runtime-verified.

The implementation is split for editing:

- `src/agent/batch_runtime.py`: stage order, queue draining, and recovery.
- `src/agent/model_lifecycle.py`: unloading, residency checks, and memory checks.
- `src/agent/docling_batch_worker.py`: isolated extraction and manifest journaling.
- `src/agent/startup_orchestration.py`: MainAgent decisions, results and bounded retries.
- `src/agent/startup_tools.py`: available startup tools, prerequisites and argument validation.
- `src/agent/redis_recovery.py`: connection diagnosis and scoped Redis service repair.

## Runtime settings

- `batch_processing=True` is enabled locally. Follow the stage and lifecycle
  rules above. The following streaming-specific behavior applies when it is false.

- `manifest_capacity=50`: maximum unfinished manifests buffered by Docling.
  Processing always starts with the first available manifest. `manifest_minimum`
  remains a legacy configuration field (default 1); the streaming graph does not
  wait for that threshold.
- `writer_batch_size=1`: the streaming graph commits one fully ready manifest
  per transaction immediately. Larger values remain supported by standalone
  batch-writing APIs, not as a delay in the streaming graph.
- `model_batch_size=50`: maximum jobs per external model consumer turn.
  External VLM and formatter consumers run continuously and independently.
  Managed model groups take one job per queue per turn to avoid a batch barrier;
  hardware limits can still require them to take turns.
- `recursive=False` and `extraction={}`: input traversal and Docling
  `ExtractionConfig` options.
- `memory_reserve_gib=2`: reserve for extraction and other overhead. Adjust it to
  measured peak usage for the actual workload.
- Per model, `requests_per_model=1` bounds concurrent inference calls;
  `formatter_adapter="chat"` or `"nuextract"` selects the endpoint protocol.
  For chat formatters, `formatter_response_format` selects `"json_schema"`,
  `"json_object"` or `"text"` according to endpoint support. The configured local
  Ollama formatters use `"json_schema"`. Unsupported endpoints must be configured
  explicitly; the runtime does not silently weaken the requested constraints.
  Parsing accepts bare JSON or a single enclosing Markdown fence, then performs
  the same strict schema validation and retains the original response.
- `serving="external"` uses a separately provisioned endpoint. For local model
  lifecycle management, use `serving="managed"`, `launch_command=[...]`, and
  verified RAM/VRAM estimates. Set `gpu_index` for the intended GPU. Arguments
  are executed without shell expansion.
  Configure the endpoint environment variable to match that process. The
  MainAgent must be externally served for streaming startup routing. Batch
  execution can also use a managed MainAgent during its isolated routing turns.

See [MAIN_AGENT.md](../MAIN_AGENT.md) for execution and recovery. These settings
are included in each run's immutable database configuration snapshot.
