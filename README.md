# Figure parser

The local configuration uses the installed `qwen3-abliterated:latest` as its
tool-calling MainAgent, with Qwen2.5-VL and Qwen2.5 for vision and formatting
through Ollama. See [setup and schema](configuration/test-pdf.md) for the model aliases,
endpoint, extracted fields, and retry policies.

The [additional model configuration](configuration/additional-models.md) adds
Qwen3-VL 2B and Granite Vision 2B readers, plus Qwen2.5 3B and Llama 3.2 3B
formatters. MainAgent thinking-based routing is enabled for both stages. The new
VLMs' requested under-4-GB peak RAM usage remains unverified; testing is paused.

Extract chart and table images from PDFs with Docling, read them with configured
vision models, format their observations against approved schemas, and store
whole documents atomically in SQL.

## Configure and run

1. Install dependencies with `uv sync` and configure `.env` from `.env.example`.
   Supply `REDIS_URL`, `DATABASE_URL`, and the endpoint/credential environment
   variables named by your model configurations.
2. Follow [configuration/README.md](configuration/README.md) to fill
   `src/config.py` with the workload, model registry, hardware estimates, routing
   and retry policies. This checkout now contains the approved English chart-test
   setup; revisit those choices for a different workload. Models must expose the
   configured chat or NuExtract protocol.
3. Define a formatter/SQL bundle using [schemas/README.md](schemas/README.md).
   For a fresh database, create the shared tables with
   `parser.src.db.tables.metadata.create_all(engine)`. Existing databases require
   the SQL migrations in `migrations/`, in order. Run creation registers the
   approved bundle and saves an immutable configuration snapshot.
4. Start Redis and externally served model endpoints, then execute:

   ```bash
   uv run python -m agent /path/to/pdfs
   ```

Use `--run-id ID` for an approved run created with `start_run()` that has not yet
started processing. For interruption recovery, use the checkpointed graph API
and resume the same checkpoint as described in [MAIN_AGENT.md](MAIN_AGENT.md).
The CLI does not install a durable checkpointer.

## Workflow

The Studio graph now shows the actual stage sequence and both routing branches:
Docling → MainAgent routing or direct routing → VLM → MainAgent formatter
routing or direct routing → formatter → field mapping → database. The graph
definition is exported in [main_agent_graph.mmd](architecture/main_agent_graph.mmd).
VLM, formatter and SQL operations execute inside those named nodes. Docling
continues producing in the background while bounded batches move through them.

The MainAgent loads model and schema context from the run's database snapshot.
Its model/tool loop selects chart → VLM and VLM → formatter mappings, creates run-specific model
queues, and launches Docling. Processing starts at `manifest_minimum` ready
manifests (1 in the local test setup; default 20), or when a smaller input
finishes. The active manifest buffer is capped at 50.

Each image is routed using configured mappings or MainAgent reasoning over its
caption, mentions, nearby text, and the top three classification predictions.
VLMs produce unrestricted observations. Formatters validate those observations
against the run's approved schema and preserve unmapped observations. Separate
retry counters prevent a formatter retry from repeating successful VLM work.

A resource scheduler drains compatible model queues concurrently. Configured
managed processes are loaded and stopped within current RAM/VRAM budgets;
external model endpoints use per-model request limits. A single model stage
always routes directly; the MainAgent continues to orchestrate that run.
An explicit `main_agent=None` runs setup and routing deterministically in the same stage graph.

Thinking-enabled stages prepare queues for all configured candidates. Both
routing flags are independent; each direct branch skips per-item LLM routing.
Models can run concurrently within a stage; each batch finishes its VLM stage
before entering its formatter stage.

The writer commits complete documents in batches of up to 50 and flushes the
final smaller batch after upstream work finishes. `approve_with_fails=False`
blocks documents containing failed images; `"omit"` stores failed identities with
empty extracted fields and retains successful images. Raw observations, model
responses and unmapped values accompany successful permanent records. Redis
retains retry and failure evidence. Run outcomes distinguish completion, item
errors, and fatal failures.

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

Tests are currently paused at the user's request. The new explicit-stage graph
has only been exported for visual inspection, not exercised end to end. Start a
new run/thread after this topology change; older coordinator checkpoints require
their corresponding old graph implementation.

Five scientific PDFs from the
[PDFFigures2 benchmark](https://github.com/allenai/pdffigures2/tree/master/evaluation)
are available locally in `testing_data/pdffigures2/`; `sources.json` records their
URLs and hashes. These are extraction test samples, not training data. Downloaded
test datasets belong under `testing_data/`. Generated images and downloaded PDFs
are ignored by Git.
