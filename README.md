# Figure parser

The local configuration uses the installed `qwen3-abliterated:latest` weights
through the `parser-main-agent:latest` alias (16,384-token context) as its
tool-calling MainAgent, with Qwen2.5-VL and Qwen2.5 for vision and formatting
through Ollama. See [setup and schema](configuration/test-pdf.md) for the model aliases,
endpoint, extracted fields, and retry policies.

The [additional model configuration](configuration/additional-models.md) adds
Qwen3-VL 2B and Granite Vision 2B readers, plus Qwen2.5 3B and Llama 3.2 3B
formatters. One MainAgent handles both routing stages with `think_sorting=True`. The new
VLMs' requested under-4-GB peak RAM usage remains unverified; testing is paused.

Extract chart and table images from PDFs with Docling, read them with configured
vision models, format their observations against approved schemas, and store
whole documents atomically in SQL.

## Configure and run

1. Install dependencies with `uv sync` and configure `.env` from `.env.example`.
   Supply `REDIS_URL`, `DATABASE_URL`, and the endpoint/credential environment
   variables named by your model configurations.
2. On first setup (`SETUP_COMPLETED=False`) or when `RERUN_SETUP=True`, ask the
   coding assistant to follow [configuration/README.md](configuration/README.md) and fill
   `configuration/run.toml` and `configuration/models.toml` with the workload, model registry, hardware estimates, routing
   and retry policies. This checkout now contains the approved English chart-test
   setup; revisit those choices for a different workload. Models must expose the
   configured chat or NuExtract protocol.
3. Define a formatter/SQL bundle using [schemas/README.md](schemas/README.md).
   For a fresh database, create the shared tables with
   `parser.src.db.tables.metadata.create_all(engine)`. Existing databases require
   the SQL migrations in `migrations/`, in order. Run creation registers the
   approved bundle and saves an immutable configuration snapshot.
4. Before launching on a RAM-limited machine, follow the
   [batch-processing requirement](#batch-processing-for-limited-ram) below.
   Make Postgres and the MainAgent endpoint available, and provision Redis.
   Startup can start the configured Redis container if it already exists but
   is stopped. Then execute:

   ```bash
   uv run python -m agent /path/to/pdfs
   ```

Use `--run-id ID` for an approved run created with `start_run()` that has not yet
started processing. For interruption recovery, use the checkpointed graph API
and resume the same checkpoint as described in [MAIN_AGENT.md](MAIN_AGENT.md).
The CLI does not install a durable checkpointer.

## Settings you can edit

| File | Purpose |
| --- | --- |
| [configuration/run.toml](configuration/run.toml) | Batch processing, thinking-based routing, retries, startup limits, Redis recovery, workload and schema settings |
| [configuration/models.toml](configuration/models.toml) | Model IDs, MainAgent selection, routing maps, model limits and hardware observations |
| `.env` | Credentials and service URLs; see [.env.example](.env.example) |
| [src/config.py](src/config.py) | Python defaults, validation and setup bookkeeping; ordinary settings belong in TOML |

Both TOML files include comments. Use lowercase `true` / `false` for switches.
For example, set `batch_processing = true` to run models sequentially, or set
`enabled = false` under `[redis_recovery]` to disable automatic Redis repair.
`think_sorting = false` uses the configured model routes; it leaves startup
orchestration enabled. To disable MainAgent completely, also set
`main_agent = ""` in `models.toml`.

`show_thinking = true` prints short, timestamped progress messages, focused on
MainAgent startup actions and model selections. It also reports batch stage
handoffs and the final outcome, without printing VLM/formatter responses or
internal reasoning. It is enabled in the local profile; set it to `false` for
quiet progress. This switch is independent of `think_sorting` and does not change
model behavior. For example:

```text
[14:32:05] MainAgent: choosing the next startup action.
[14:32:21] MainAgent: selected to check the Redis connection.
[14:32:21] Startup finished: check the Redis connection.
[14:33:10] MainAgent: choosing a VLM for an image.
[14:33:24] MainAgent selected chart_reader for the VLM stage.
```

Settings are loaded afresh for each new run and saved in its database snapshot.
Resuming a run never reads these files. The existing settings were preserved
when they moved out of Python. Misspelled settings and invalid combinations
fail validation before a new run is created.

For another run profile, use `--config configuration/custom-run.toml`. Its
`models_file` path is relative to that TOML file; other project paths retain
their existing meaning, so launch from the project root. `--config` and
`--run-id` cannot be combined.

## MainAgent startup

After loading the saved configuration, MainAgent selects startup tools: check
Redis, set model routes, create queues, schedule models, and start the pipeline.
Each tool returns its result or operational error to the agent. The graph shows
these tools branching from `main_agent_startup` and returning through
`startup_tool_result`, rather than a fixed sequence of Python startup steps.

If Redis fails, MainAgent can inspect connectivity, service status and recent
logs using `diagnose_redis`. When the configured local Redis container is
stopped, `start_redis_service` starts it and verifies connectivity. Recovery is
bounded and does not depend on Redis queues. Unsupported problems stop with a
reported cause. See [startup recovery configuration](configuration/README.md#mainagent-startup-and-recovery).

## Batch processing for limited RAM

**Instruction to coding assistants:** use `batch_processing=True` for the
user's RAM-limited local setup. Reuse the approved models, routing choices, and
schema. This mode must run one model at a time, completing its assigned work
for a bounded batch and releasing its RAM/VRAM before loading the next model.

`batch_processing = true` is enabled in `configuration/run.toml`. The batch runtime
waits for each stage to finish and unloads its models before advancing. Docling
runs in a separate process that exits after each batch. Local external models
use `lifecycle="ollama"` for explicit unloading and residency checks. This mode
has been inspected statically; tests and live runs remain paused.
`model_batch_size` and `requests_per_model=1` alone do not provide this behavior.

Startup MainAgent decisions run separately and unload the model before tool
execution. After startup, the order for each batch is:

```text
Docling → release extraction models
  → if think_sorting: MainAgent assigns VLM queues → unload MainAgent
  → first assigned VLM → unload → next assigned VLM → unload → …
  → if think_sorting: MainAgent assigns formatter queues → unload MainAgent
  → first assigned formatter → unload → next assigned formatter → unload → …
  → validate and write complete manifests → next batch
```

With `think_sorting=False`, per-image routing uses saved maps without loading
MainAgent. A configured MainAgent still owns startup; `main_agent=None` is the
explicit opt-out from all MainAgent calls.
A stage with only one candidate also routes directly. Run only models with
assigned work; each image goes to its selected model. Save intermediate results
between stages, preserve retry limits, and keep document writes atomic.

The configuring assistant must follow the detailed
[low-memory execution requirements](configuration/README.md#low-memory-batch-processing).
Those include unloading local models served through external endpoints and
keeping Docling, routing, VLM, and formatter execution from overlapping.
Each individual model must still fit the available memory.
Use the Ollama endpoint exclusively for this run; unrelated resident models
cause a clear error and are left untouched. Old run snapshots without
`batch_processing` retain streaming execution. Start a new run to use this mode.

## Streaming workflow (`batch_processing=False`)

The Studio graph exposes the data flow:

```text
Docling → MainAgent/direct VLM routing → VLM queues
       → MainAgent/direct formatter routing → formatter queues
       → map ready results → write a complete manifest
```

The two routing nodes share one MainAgent and one decision lock. `think_sorting`
controls both; a single-model stage routes directly. Thinking-enabled runs create
queues for all candidates. MainAgent selects startup operations through native
tools; Python validates their arguments and executes them.

Processing starts with the first available manifest. External VLM and formatter
consumers run continuously, so formatting can start while other images are still
being read. Graph nodes publish ready work and observe saved results; they do not
wait for a whole VLM batch. The loop after writing goes to `await_ready_work`,
which waits for new manifests/results without restarting Docling.
The graph is exported in [main_agent_graph.mmd](architecture/main_agent_graph.mmd).

The active manifest buffer is capped at 50. Managed model groups take short turns
within verified memory budgets; external endpoints control actual residency and
request scheduling. Workers preserve successful results and bounded retry counts.
Local formatters request schema-constrained JSON. Parsing also accepts one
Markdown fence around JSON, then validates the unchanged data against the schema.

Database writes happen per complete manifest, in one transaction. The configured
`approve_with_fails=False` blocks a manifest after an image exhausts retries,
reports its failure, and lets other manifests continue. Failure evidence stays in
Redis. No partial document is inserted.

See [MAIN_AGENT.md](MAIN_AGENT.md) for queue ownership, resource settings, recovery,
and operational limitations; [architecture/response.md](architecture/response.md)
contains the original requested workflow.

## Development

```bash
LANGSMITH_TEST_TRACKING=false LANGSMITH_TRACING=false uv run pytest tests -q
uv run langgraph dev
```

The `agent` graph is the complete pipeline; `extract_documents` is the standalone
Docling graph. Tests exercise coordinator recovery, Redis queue behavior, routing,
retry policies, schema validation and SQL transactions. Model responses and PDF
conversion are simulated in the full-pipeline tests; those tests do not benchmark
real model extraction accuracy.

Tests are currently paused at the user's request. The streaming graph
has only been inspected statically and exported, not exercised end to end. Start a
new run/thread after this topology change; older coordinator checkpoints require
their corresponding old graph implementation.

Five scientific PDFs from the
[PDFFigures2 benchmark](https://github.com/allenai/pdffigures2/tree/master/evaluation)
are available locally in `testing_data/pdffigures2/`; `sources.json` records their
URLs and hashes. These are extraction test samples, not training data. Downloaded
test datasets belong under `testing_data/`. Generated images and downloaded PDFs
are ignored by Git.
