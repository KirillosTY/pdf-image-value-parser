import json
import os
from copy import deepcopy
from uuid import uuid4

import fakeredis
import pytest
from parser.src.db import db, tables
from parser.src.redis.state import (
    DB_READY,
    load_manifest,
    mark_document_stored,
    update_manifest,
)
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.schema import CreateSchema, CreateTable, DropSchema

from schema_format import BAR_CHART, CHART_SCHEMAS

from .test_formatter import manifest_for, model_response


@compiles(JSONB, "sqlite")
def compile_test_jsonb(type_, compiler, **kwargs):
    return "JSON"


@pytest.fixture
def engine():
    # By default exercise real SQL transactions in SQLite. If provided, use an
    # isolated temporary schema in Postgres instead, never existing app tables.
    url = os.getenv("TEST_POSTGRES_URL")
    base = create_engine(url or "sqlite://")
    schema = f"test_parser_{uuid4().hex}" if url else None
    if schema:
        with base.begin() as connection:
            connection.execute(CreateSchema(schema))
        configured = base.execution_options(schema_translate_map={None: schema})
    else:
        configured = base

        @event.listens_for(base, "connect")
        def enable_foreign_keys(connection, record):
            connection.execute("PRAGMA foreign_keys=ON")

    tables.metadata.create_all(configured)
    try:
        yield configured
    finally:
        if schema:
            with base.begin() as connection:
                connection.execute(DropSchema(schema, cascade=True))
        base.dispose()


def complete_manifest(key="pdf:1", count=2):
    manifest = manifest_for(key, count)
    manifest["format_status"] = "complete"
    for image in manifest["assets"][0]["images_meta"]:
        image["vlm_status"] = image["format_status"] = "complete"
        image["vlm_result"] = {"raw_output": "original text", "model": "vlm"}
        image["formatting"] = {
            "model": "numind/NuExtract3",
            "schema": BAR_CHART,
            "model_response": model_response(),
            **json.loads(model_response()),
            "error": None,
        }
    return manifest


def count_rows(engine, table):
    with engine.connect() as connection:
        return connection.scalar(select(func.count()).select_from(table))


def test_50_pdfs_per_transaction_and_final_tail(engine):
    batches = []
    manifests = [complete_manifest(f"pdf:{i}") for i in range(51)]
    assert (
        db.write_batches(
            engine, manifests, on_committed=lambda batch: batches.append(len(batch))
        )
        == 51
    )
    assert batches == [50, 1]
    assert count_rows(engine, tables.documents) == 51
    assert count_rows(engine, tables.images) == 102
    assert count_rows(engine, tables.MEASUREMENT_TABLES["BAR_CHART"]) == 102


def test_incomplete_pdf_rejects_whole_transaction(engine):
    incomplete = complete_manifest("pdf:2")
    incomplete["assets"][0]["images_meta"][1]["format_status"] = "failed"
    with pytest.raises(ValueError, match="Every image"):
        db.write_batches(engine, [complete_manifest(), incomplete])
    assert count_rows(engine, tables.documents) == 0


def test_database_failure_rolls_back_all_images(engine):
    acknowledged = []

    def fail(connection, cursor, statement, parameters, context, many):
        if "INSERT INTO" in statement and "bar_values" in statement:
            raise RuntimeError("injected database failure")

    event.listen(engine, "before_cursor_execute", fail)
    with pytest.raises(RuntimeError, match="injected"):
        db.write_batches(
            engine, [complete_manifest()], on_committed=acknowledged.append
        )
    event.remove(engine, "before_cursor_execute", fail)
    assert not acknowledged
    assert count_rows(engine, tables.documents) == 0
    assert count_rows(engine, tables.images) == 0


def test_retry_after_commit_is_idempotent_and_marks_only_document(engine):
    client = fakeredis.FakeRedis()
    snapshot = complete_manifest()
    client.set("pdf:1", json.dumps(snapshot))
    update_manifest(client, "pdf:1", lambda manifest: None)

    def acknowledgment_fails(batch):
        raise RuntimeError("Redis temporarily unavailable")

    with pytest.raises(RuntimeError, match="Redis"):
        db.write_batches(engine, [snapshot], on_committed=acknowledgment_fails)
    assert load_manifest(client, "pdf:1")["db_status"] == "not_started"
    assert client.zcard(DB_READY) == 1
    assert db.flush_ready(client, engine, final=True) == 1
    assert count_rows(engine, tables.images) == 2
    assert count_rows(engine, tables.MEASUREMENT_TABLES["BAR_CHART"]) == 2
    saved = load_manifest(client, "pdf:1")
    assert saved["db_status"] == "complete"
    assert all("db_status" not in image for image in saved["assets"][0]["images_meta"])
    assert client.zcard(DB_READY) == 0


def test_smaller_batch_waits_for_final_signal(engine):
    client = fakeredis.FakeRedis()
    client.set("pdf:1", json.dumps(complete_manifest()))
    update_manifest(client, "pdf:1", lambda manifest: None)
    assert db.flush_ready(client, engine) == 0
    assert count_rows(engine, tables.documents) == 0
    assert db.flush_ready(client, engine, final=True) == 1


def test_changed_snapshot_cannot_be_acknowledged_or_replace_stored_data(engine):
    client = fakeredis.FakeRedis()
    snapshot = complete_manifest()
    db.write_batches(engine, [snapshot])
    changed = deepcopy(snapshot)
    changed["document_metadata"]["title"] = "changed"
    client.set("pdf:1", json.dumps(changed))
    with pytest.raises(ValueError, match="changed"):
        mark_document_stored(client, snapshot)
    with pytest.raises(ValueError, match="different content"):
        db.write_batches(engine, [changed])
    assert count_rows(engine, tables.images) == 2


def test_multi_panel_image_has_separate_chart_identities(engine):
    manifest = complete_manifest(count=1)
    formatting = manifest["assets"][0]["images_meta"][0]["formatting"]
    bar = formatting["data"]
    scatter = {"chart_type": "SCATTER_PLOT", "data": [{"x": 1, "y": 2}]}
    formatting["schema"] = {
        "type": "object",
        "properties": {"charts": {"type": "array"}},
        "required": ["charts"],
    }
    formatting["data"] = {"charts": [bar, scatter]}
    db.write_batches(engine, [manifest])
    assert count_rows(engine, tables.images) == 1
    assert count_rows(engine, tables.charts) == 2
    assert count_rows(engine, tables.MEASUREMENT_TABLES["SCATTER_PLOT"]) == 1


def test_postgres_ddl_has_numeric_measurements_and_source_links():
    for table in tables.metadata.sorted_tables:
        assert "CREATE TABLE" in str(
            CreateTable(table).compile(dialect=postgresql.dialect())
        )
    ddl = str(
        CreateTable(tables.MEASUREMENT_TABLES["BAR_CHART"]).compile(
            dialect=postgresql.dialect()
        )
    )
    assert "value NUMERIC NOT NULL" in ddl
    assert "FOREIGN KEY(chart_id) REFERENCES charts" in ddl


@pytest.mark.parametrize("kind", CHART_SCHEMAS)
def test_every_chart_family_maps_to_relational_rows(kind):
    schema = CHART_SCHEMAS[kind]

    def example(schema):
        if "const" in schema:
            return schema["const"]
        if "enum" in schema:
            return schema["enum"][0]
        if schema["type"] == "object":
            return {
                name: example(value) for name, value in schema["properties"].items()
            }
        if schema["type"] == "array":
            return [example(schema["items"])]
        return {
            "string": "source label",
            "number": 1.2,
            "integer": 1,
            "boolean": False,
        }[schema["type"]]

    manifest = complete_manifest(count=1)
    formatting = manifest["assets"][0]["images_meta"][0]["formatting"]
    formatting.update(schema=schema, data=example(schema))
    rows = db.document_rows(manifest)
    assert rows["charts"][0]["chart_type"] == kind
    assert any(
        name not in {"documents", "images", "charts", "extraction_details"}
        for name in rows
    )
