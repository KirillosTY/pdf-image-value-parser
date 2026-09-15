# Proposed local chart comparison setup

Status: prepared for the configuration README's final schema/policy review.
The user approved the installed local models, English test-PDF workload, and
accuracy preference. Active configuration and Ollama models have not changed.

## Files to activate

| Prepared file | Destination / purpose |
| --- | --- |
| [config.py](config.py) | Complete proposed replacement for `src/config.py`; dataclasses unchanged |
| [bundle.json](bundle.json) | `schemas/test-pdf/bundle.json` |
| [vlm.Modelfile](vlm.Modelfile) | Local alias `parser-chart-vlm:7b` from installed `qwen2.5vl:7b` |
| [formatter.Modelfile](formatter.Modelfile) | Local alias `parser-chart-formatter:7b` from installed `qwen2.5:7b` |
| [endpoint.env](endpoint.env) | Add `OLLAMA_BASE_URL` to `.env`, preserving other settings |
| This document | `configuration/test-pdf.md`, updating relative links and activation status |

Both models use the running local Ollama OpenAI-compatible endpoint. The VLM
accepts chart images and returns unrestricted text. The text formatter accepts
that text with the schema using the existing `chat` adapter. No MainAgent is
needed with one model per stage.

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

[Full example JSON](example-output.json), [mapped SQL rows](example-sql-rows.json),
and [compiled SQL layout](schema.sql) are provided. The example was authored
from three known bar-chart values to demonstrate the schema; it is not model
output or evidence of extraction accuracy.

| panel | chart_type | series | category | value | unit | reading |
| --- | --- | --- | --- | ---: | --- | --- |
| 1 | bar_chart | North | Jan | 120 | thousand USD | printed |
| 1 | bar_chart | South | Jan | 95 | thousand USD | printed |
| 1 | bar_chart | North | Feb | 150 | thousand USD | printed |

## Proposed policies and verification

- Three additional retries independently for vision and formatting.
- A terminal image failure blocks the entire document's database write.
- Start at one manifest and write one whole document per transaction, suitable
  for this single-PDF test. Buffer capacity and model queue turns remain 50.
- Preserve all accepted chart/table crops for diagnostics. For numerical
  scoring, process chart pages 2–8 only, and keep the page 9 answer key and other
  answer-bearing context out of both models' inputs.
- No database or Redis provisioning is included in this model setup. A direct
  crop-to-VLM-to-formatter comparison can run without either service; testing
  full pipeline persistence additionally requires their endpoints.

`validate.py` checks the complete proposed config, JSON Schema, example output,
and SQL row mapping without connecting to services or creating tables. Results
are saved in [validation.json](validation.json). Numerical extraction and
runtime memory use have not been tested yet.

After activation, run each retained chart crop through the real extraction and
formatting functions, save both outputs, compare labels and numeric values to
the fixture source data, and report missing/extra values as well as numerical
errors. Schema validation alone must not be reported as numeric correctness.
