# Formatting and Postgres storage

Approved behavior: NuExtract formats saved, unrestricted VLM text using a
caller-selected predefined schema. Redis holds processing progress and results.
Postgres stores complete PDFs in batches of at most 50 PDFs. A document's images
are never split across transactions. `db_status` exists only on the document.

## Manifest fields

Each accepted image has:

- `vlm_status`: `not_started`, `processing`, `complete`, or `failed`.
- `vlm_result`: original `raw_output` text and optional VLM model name.
- `format_status`: `not_started`, `processing`, `complete`, or `failed`.
- `formatting`: attempt ID, model, supplied schema, original formatter response,
  validated data, unmatched observations, and any error.

The document has an aggregate `format_status` and a `db_status` of `not_started`
or `complete`. Failed database transactions leave `db_status` unchanged.

`save_vlm_result()` saves raw text, VLM completion, and an image job in the
`images:format_ready` Redis Stream in one transaction. The job contains the
manifest and image keys; payloads remain in the manifest. A saved enqueue marker
prevents duplicate jobs when the caller retries after a lost transaction reply.
The formatter requires that saved result. The existing VLM inference function still accepts
image bytes and returns unrestricted text; its future queue consumer must call
this helper after inference and report processing/failure through the manifest
update helper. No schema is passed into the original VLM.

## Calling the formatter

Use an independently served NuExtract endpoint supporting its custom chat
template arguments. The implementation follows the
[NuExtract3 text extraction API](https://huggingface.co/numind/NuExtract3#vllm-inference-structured-extraction-text).
Set `FORMATTER_BASE_URL`, optionally `FORMATTER_MODEL` and `FORMATTER_API_KEY`.
The endpoint is not started or downloaded by this project.

```python
from parser.src.formatter.model import NuExtractFormatter
from parser.src.formatter.worker import format_image
from parser.src.redis.state import red, save_vlm_result
from schema_format import BAR_CHART

# Use the actual manifest/image keys returned by extraction and actual VLM text.
save_vlm_result(red, manifest_key, image_key, raw_vlm_text, model=vlm_model)

formatter = NuExtractFormatter.from_env()
format_image(
    red,
    formatter,
    manifest_key=manifest_key,
    image_key=image_key,
    schema=BAR_CHART,
)
```

`format_image()` coordinates `lock_n_update()` and `start_formatting()`.
The former keeps an image lock while the latter runs inference and saves the
outcome. Redis WATCH retries merge updates for different images in a manifest.
Attempt IDs prevent superseded workers from publishing results.

The model uses a template converted from the schema; the unchanged original
schema validates its answer. Normalization traverses objects/arrays, converts
explicit boolean strings, and omits optional null fields. It does not fill
missing measurements. Required missing data, malformed JSON, duplicate JSON
keys, non-finite numbers, and truncated responses fail explicitly. There is one
model attempt per call; retries are explicit. Completed images cannot be
reformatted in place. Create a new extraction attempt for changed results.

A stopped process can leave `processing`; after its 600-second lock expires,
explicitly retry the call. The default model client uses a 120-second timeout
with no automatic HTTP retry. This timeout is per client network operation, not
a guarantee of total wall time. Output is capped at 16,384 tokens and truncation
is a failure. A custom injected client must also be configured appropriately.

Raw VLM text and the original formatter response are preserved. An empty
`unmapped_observations` list is a model claim, not proof of complete extraction.

## Database eligibility and batches

## Document identity and run recovery

Docling always opens and extracts every input PDF. After extraction, it builds
`document_id` from the normalized metadata fields in this fixed order:

```text
doi, title, authors, organization, publication_date/publication_year
```

Missing fields remain empty. The byte-level `pdf_sha256` is retained separately
for provenance, but it is not the document identity used for run recovery. If
all identity metadata is missing, the fallback uses `run_id` and the basename
of the input file. The exact run and metadata identity are saved in the
manifest and the Postgres `documents` row.

`run_info` records `run_id` and whether the run is `incomplete` or `complete`.
On a resumed run only, after Docling has extracted metadata, the worker asks
Postgres whether the same `run_id` and `document_id` are already committed. A
match returns `already_stored` and does not publish that newly extracted
manifest to the downstream queue. A new run does not perform this lookup, so
the same paper can be included in multiple batches. Filenames alone are never
used to match documents.

```python
from parser.src.db.runs import start_run

run_id = start_run(engine)
worker = DocumentWorker(run_id=run_id)
worker.process_folder(input_path, database_engine=engine, resume=False)
```

For recovery, pass the existing unfinished `run_id` and `resume=True`. The
caller must first verify it with `require_incomplete_run()`. After all work and
the final database batch are complete, call `complete_run()`.

The final image completion atomically adds its manifest key to the Redis sorted
set `documents:db_ready`. Eligibility requires a successful Docling document,
at least one accepted image, and complete VLM/formatting for every image.
One failed image holds back the whole PDF. Below-threshold images are excluded.

```python
import os
from sqlalchemy import create_engine
from parser.src.db.db import flush_ready
from parser.src.redis.state import red

engine = create_engine(os.environ["DATABASE_URL"])
batch_size = int(os.getenv("DB_BATCH_SIZE", "50"))

# Normal operation: a smaller batch waits for more complete PDFs.
flush_ready(red, engine, batch_size=batch_size)

# Only when production and formatting are finished: drain the smaller tail too.
while flush_ready(red, engine, batch_size=batch_size, final=True):
    pass
```

This is a callable batch writer. The caller is responsible for scheduling it
and knowing when production/formatting have ended. The main agent's service
hooks and event transport remain unwired. Formatting Stream consumption, pending
job reclamation, and acknowledgment still need wiring; publishing the job does
not itself start a formatting worker.

The writer selects at most 50 manifests and writes all their rows in one
transaction. Individual SQL statements are capped at 500 rows, but all statements
for that batch share the transaction. The 50-PDF limit does not bound image count
or bytes, and a large PDF can still create a large transaction.

Only after commit does the writer mark each document's `db_status` complete and
remove it from the ready set. If Redis acknowledgment fails, ready documents
remain available. PostgreSQL conflict handling makes identical retries safe.
A snapshot fingerprint rejects reuse of a manifest identity for different data.
Acknowledgment also checks that Redis still contains the committed snapshot.
Redis and Postgres do not share a distributed transaction.

## Relational tables

The table definitions are in `src/db/tables.py`. SQLAlchemy Core uses psycopg;
no ORM sessions or per-image ORM instances are required.

| Table | Queryable content |
| --- | --- |
| `run_info` | Batch identity and `incomplete`/`complete` recovery status |
| `documents` | PDF attempt key, document hash/ID, title, publication year, DOI, source |
| `images` | Source document, asset, page, dimensions, raw VLM text |
| `charts` | Separate chart identity, image reference, chart type, optional panel label, axes, units |
| `bar_values`, `line_values`, `area_values`, `radar_values`, `combo_values` | Category, series, numeric value, source ordinal |
| `pie_values`, `funnel_values` | Label and numeric value |
| `scatter_points`, `bubble_points` | Numeric coordinates and optional series; bubble size |
| `histogram_bins` | Bin boundaries and count |
| `box_statistics` | Group, minimum, quartiles, median, maximum |
| `heatmap_cells` | Coordinates and numeric value |
| `candlestick_values` | Printed date label and OHLC values |
| `waterfall_values` | Label, numeric value, total flag |
| `flow_nodes`, `flow_edges` | Node identifiers/labels and directed connections |
| `combo_series` | Per-series render type and axis assignment |
| `unknown_observations` | Unclassified observations as text rows |
| `extraction_details` | Formatter model, schema, exact response text, JSONB data and unmatched observations |

Measurement columns derive from the existing predefined schemas, using SQL
numeric/string/boolean types. Table names come from a fixed registry, never
from unrestricted model output. JSONB retains supplementary metadata and the
full formatted result; measurements also exist in typed columns for analysis.
Each PDF extraction attempt has its own manifest identity, so separate retries
of extraction remain distinguishable from retries of the database write.

One image can map to multiple charts if the caller supplies a predefined schema
with a `charts` array. Each member must validate against a registered chart
schema. The existing schemas still describe one chart each; this change does
not infer panel splitting or introduce new measurement/time fields.

A query for exactly ten measurement rows in an ordinary, unstacked bar chart:

```sql
SELECT c.chart_id, c.title, c.unit
FROM charts AS c
JOIN bar_values AS b ON b.chart_id = c.chart_id
WHERE c.chart_type = 'BAR_CHART' AND c.stacking = 'none'
GROUP BY c.chart_id, c.title, c.unit
HAVING COUNT(*) = 10;
```

Ten rows are not necessarily ten categories or ten visible pillars in a grouped
or stacked chart. `ordinal` preserves extraction order; it does not claim a
chronological order. Categories remain printed labels, and publication year is
not the time represented by a chart. A later schema decision is needed for
explicit measured-time and bar/segment identities. No dates are guessed here.

## Explicit database setup

`DATABASE_URL` uses `postgresql+psycopg://...`. Imports and engine construction do
not create tables. On a new, intended database, explicitly run:

```python
from parser.src.db.tables import metadata
metadata.create_all(engine)
```

This creates missing tables; it is not a migration mechanism for changing
existing tables. No application database or model endpoint is created by tests.

## Verification

```bash
LANGSMITH_TEST_TRACKING=false LANGSMITH_TRACING=false uv run pytest \
  tests/unit_tests/test_formatter.py tests/unit_tests/test_db_batches.py -q
```

The default checks use a simulated Redis server and actual SQLite transactions
with test-only JSONB compilation. Set `TEST_POSTGRES_URL` to run the database
checks against Postgres in isolated, randomly named temporary schemas. The test
role needs schema creation permission; each test removes only its own schema.
Model calls are mocked, so these checks establish pipeline behavior, not actual
NuExtract extraction accuracy.
