# Local chart comparison setup

Status: approved and activated. The user approved the local models, English
test-PDF workload, schema, and retry/failure policies. See the comparison report
for measured results; the historical proposal remains in `proposed-test-pdf/`.

The baseline described below has since been expanded with two more VLMs and two
more formatters; [additional-models.md](additional-models.md) describes the
current registry. Both routing-thinking flags are now enabled. Historical
fixture results do not evaluate these additions.

## Configuration files

| Prepared file | Destination / purpose |
| --- | --- |
| [config.py](../src/config.py) | Complete proposed replacement for `src/config.py`; dataclasses unchanged |
| [bundle.json](../schemas/test-pdf/bundle.json) | `schemas/test-pdf/bundle.json` |
| [vlm.Modelfile](proposed-test-pdf/vlm.Modelfile) | Local alias `parser-chart-vlm:7b` from installed `qwen2.5vl:7b` |
| [formatter.Modelfile](proposed-test-pdf/formatter.Modelfile) | Local alias `parser-chart-formatter:7b` from installed `qwen2.5:7b` |
| [endpoint.env](proposed-test-pdf/endpoint.env) | Add `OLLAMA_BASE_URL` to `.env`, preserving other settings |
| This document | `configuration/test-pdf.md`, updating relative links and activation status |

Both models use the running local Ollama OpenAI-compatible endpoint. The VLM
accepts chart images and returns unrestricted text. The text formatter accepts
that text with the schema using the existing `chat` adapter.

The MainAgent now uses the separately installed `qwen3-abliterated:latest`
(ID `b07c3bcda724`) at the same endpoint, as requested by the user. Local metadata
reports 8.2B parameters, Q4_K_M, a 40,960-token model context and native tools.
The user referred to this installed model as `qwen3.8-abliterated`; the exact
Ollama model ID is used in `src/config.py`. One model per stage disables routing
reasoning but leaves the tool-calling orchestrator active. No new weights were
downloaded. Context allocation and real tool execution remain unverified because
the user paused testing. Previous fixture results predate this MainAgent change.

Ollama's official cards identify the installed models as Q4_K_M, 8.29B total
parameters for the VLM and 7.62B for the text model. The VLM is intended for
chart/image understanding; the text model supports instruction-following and
JSON output. This establishes protocol suitability, not fixture accuracy.

Sources: [VLM model card](https://huggingface.co/Qwen/Qwen2.5-VL-7B-Instruct),
[installed VLM variant](https://ollama.com/library/qwen2.5vl:7b),
[text formatter variant](https://ollama.com/library/qwen2.5:7b),
[Ollama API and context configuration](https://docs.ollama.com/api/openai-compatibility).

The aliases set 8,192 context / 4,096 maximum output tokens for the VLM and
16,384 context / 8,192 maximum output tokens for the formatter. The app sends
the output limits, while Modelfiles enforce context sizes. A config field alone
does not change Ollama's context. Creating aliases reuses installed weights:

```bash
ollama create parser-chart-vlm:7b -f configuration/proposed-test-pdf/vlm.Modelfile
ollama create parser-chart-formatter:7b -f configuration/proposed-test-pdf/formatter.Modelfile
```

Hardware was measured outside the sandbox on 2026-09-14 at 11:22:53 UTC:
Intel Core Ultra 9 185H, 22 logical CPUs, 30.45 GiB total / 18.97 GiB available
RAM; RTX 4060 Laptop GPU, 8.00 GiB total / 7.65 GiB available VRAM. The full
timestamped measurement is in the proposed config. Recheck before inference.
Peak memory with these contexts and crops is unmeasured, so estimates stay
`None`. Ollama manages loading, eviction, and possible CPU offload. The app's
managed-process memory guard does not enforce a VRAM budget on this external
server. Simultaneous GPU residency of both models is not assumed. Each model
has one concurrent request; actual peak memory and latency need a smoke test.

## Values and SQL

The `records-v1` bundle uses one `MEASUREMENTS` schema for every accepted Docling
label. This avoids forcing a heatmap into a scatter schema or dropping the
second panel because of the classifier's first label. The formatter follows
the VLM observations when assigning chart type; this cannot repair an incorrect
VLM reading.

Each measurement preserves panel, chart type, title, series, category, heatmap
row label, numeric x/y, numeric value, box statistic, literal unit, reading
status, and notes. Bar/line/pie/heatmap values use `value`; scatter points use
`x` and `y`; box plots use one row per visible statistic. Missing fields are
explicit JSON null / SQL NULL. Printed and estimated readings stay distinct.
Whiskers are not assumed to be sample minima/maxima. No unit conversion is
performed: `120` thousand USD remains `120` with unit `thousand USD`.

The physical extracted table is
`extracted_50542627ef06c434_measurements`. Its composite primary key is
`(image_key, ordinal)`; `image_key` references shared `images`. Numeric fields
use PostgreSQL NUMERIC; labels and units use TEXT. Panel, chart type, and reading
status are required. Shared image/document/run tables retain provenance.
Unrepresentable observations remain in the formatter envelope's
`unmapped_observations`, alongside the saved raw VLM output.

[Full example JSON](proposed-test-pdf/example-output.json), [mapped SQL rows](proposed-test-pdf/example-sql-rows.json),
and [compiled SQL layout](proposed-test-pdf/schema.sql) are provided. The example was authored
from three known bar-chart values to demonstrate the schema; it is not model
output or evidence of extraction accuracy.

| panel | chart_type | series | category | value | unit | reading |
| --- | --- | --- | --- | ---: | --- | --- |
| 1 | bar_chart | North | Jan | 120 | thousand USD | printed |
| 1 | bar_chart | South | Jan | 95 | thousand USD | printed |
| 1 | bar_chart | North | Feb | 150 | thousand USD | printed |

## Policies and verification

- Three additional retries independently for vision and formatting.
- A terminal image failure blocks the entire document's database write.
- Start at one manifest and write one whole document per transaction, suitable
  for this single-PDF test. Buffer capacity and model queue turns remain 50.
- Preserve all accepted chart/table crops for diagnostics. For numerical
  scoring, process chart pages 2–8 only, and keep the page 9 answer key and other
  answer-bearing context out of both models' inputs.
- The full app uses isolated Docker test services from
  [test-services.compose.yml](test-services.compose.yml): Postgres on localhost
  port 15432 and Redis on localhost port 16379. Credentials and connection URLs
  are saved in ignored `.env`; the existing database on port 5432 is untouched.
  Named volumes retain test results across container restarts.

`validate.py` checks the complete proposed config, JSON Schema, example output,
and SQL row mapping without connecting to services or creating tables. Results
are saved in [validation.json](proposed-test-pdf/validation.json). Numerical extraction and
runtime memory use have not been tested yet.

The full app test starts from `output/pdf/chart_extraction_blind_input.pdf`,
which contains original pages 2–8 without the answer key. It runs Docling,
VLM, formatting, real Redis queues and real SQL writes through the normal CLI:


```bash
docker compose --env-file .env -p parser-chart-test -f configuration/test-services.compose.yml up -d --wait
.venv/bin/python -m agent output/pdf/chart_extraction_blind_input.pdf
```

Shared database tables were initialized once with `metadata.create_all(engine)`;
each run registers its versioned measurement table. The app log is in
`output/pdf/full_pipeline.log`, and exported results are under
`output/pdf/full_pipeline/<run_id>/`. The comparison scores committed SQL values
against independently held fixture source data. Schema validation alone is not
numeric correctness.

Stop the isolated services while retaining their data with:

```bash
docker compose --env-file .env -p parser-chart-test -f configuration/test-services.compose.yml stop
```
