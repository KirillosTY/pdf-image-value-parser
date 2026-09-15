# Approved formatter and SQL schemas

The setup LLM prepares `schemas/<project>/bundle.json` after user approval.
`default/bundle.json` captures the existing chart schemas and SQL layout using
the `charts-v1` mapping. That mapping is fixed; use `records-v1` for custom data.
`example-business/bundle.json` is an illustrative custom schema, not a selected
production configuration.

Each bundle has a name, mapping, `formatter_schemas`, and `sql_tables`.
`records-v1` tables declare:

| Field | Meaning |
| --- | --- |
| `schema_keys` | Formatter schema keys whose results populate this table |
| `records_path` | Object-property path selecting a record object or record array |
| `columns` | Column definitions with SQL type, source path, nullability, optional description/unit |
| Column `source` | Property path relative to each record; `[]` selects the record itself |

Supported SQL types are `text`, `numeric`, `integer`, `boolean`, and `json`.
Paths are lists of property names; they are never evaluated as Python or SQL.
Missing optional values become SQL NULL; missing required values reject the
whole document transaction. Units/descriptions can be included in both the JSON
Schema and column specification and are preserved in the versioned definition.

Every custom table gets `(image_key, ordinal)` as its primary key and a foreign
key to `images`. Physical names are
`extracted_<first_16_characters_of_schema_id>_<logical_table_name>`.
Find actual names using `output_tables(bundle)`; obtain the bundle from the
run's DB context. Relations between runs, documents, and images are shared;
custom records join through `image_key`. Additional custom foreign-key layouts
need a reviewed mapping extension rather than arbitrary SQL from model output.

The full validated formatter result and unmapped observations remain in
`extraction_details`, including values not selected into custom SQL columns.
Failed images under `"omit"` retain an image row with `failed = true` and an
empty extraction-details row. Their identity/page/dimensions remain known.

## Initialize and start a configured run

Run `uv sync` after updating the project. Set DATABASE_URL in your environment
or load your `.env` explicitly in a script. The following writes to Postgres:

```python
import os
from sqlalchemy import create_engine
from config import CONFIG
from parser.src.db.tables import metadata
from parser.src.db.runs import start_run
from parser.src.docling.docling_tool import DocumentWorker

engine = create_engine(os.environ["DATABASE_URL"])
metadata.create_all(engine)  # Fresh databases only; existing columns need migration.
run_id = start_run(engine, config=CONFIG)
worker = DocumentWorker(run_id=run_id)
worker.process_folder("/path/to/pdfs", database_engine=engine)
```

The existing standalone graph also accepts `pipeline_config=CONFIG.snapshot()`
alongside its Docling `config`, input path, and `database_url`. On recovery pass
the existing `run_id` and `resume=true`; the stored configuration is used.
The separate main `agent` graph still has unwired service hooks.

Existing databases need `migrations/001_run_schema_configuration.sql` and
`migrations/002_run_outcome.sql` before
using the new code. `metadata.create_all()` does not alter existing columns.
Legacy runs have no known configuration/schema snapshot and are left unbound;
recovery rejects them until their actual schema/configuration is explicitly
identified and attached. The migration never guesses a historical schema.

## Format and store using the run schema

```python
from parser.src.db.runs import load_run_context
from parser.src.formatter.worker import format_image

context = load_run_context(engine, run_id, resume=True)
# formatter is an already-configured adapter; client is the Redis client.
format_image(
    client, formatter,
    manifest_key=manifest_key, image_key=image_key,
    database_engine=engine,
    # Optional schema_key="BUSINESS"; otherwise use the run's schema_matcher/fallback.
)
```

An explicit schema must match the run's registered schema. Legacy standalone
formatting without a registered run still accepts a caller-supplied `schema`.
No schema is sent to the VLM.

After the caller exhausts retries for an image whose VLM or formatting status
is `failed`, call `mark_image_failed(client, manifest_key, image_key)`. This sets
the terminal `failed` flag. A failed attempt awaiting retry does not qualify for
`"omit"`. The final image update publishes document readiness atomically in
Redis. Existing `flush_ready(client, engine)` batches whole documents, and
`final=True` drains the smaller final batch. No automatic retry scheduler or
worker launcher is introduced by this setup change.
