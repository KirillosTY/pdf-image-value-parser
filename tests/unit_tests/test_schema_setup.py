import json
from copy import deepcopy

import fakeredis
import pytest
from parser.src.db import db, runs, tables
from parser.src.db.schemas import default_bundle, load_bundle, output_tables, schema_id
from parser.src.formatter.worker import format_image
from parser.src.redis.state import (
    DB_READY,
    load_manifest,
    mark_image_failed,
    save_vlm_result,
    update_manifest,
)
from sqlalchemy import select

from .test_configuration import configured
from .test_db_batches import complete_manifest, count_rows
from .test_db_batches import engine as engine
from .test_formatter import formatter_for, manifest_for, model_response


def business_bundle():
    return {
        "name": "business-values-v1",
        "mapping": "records-v1",
        "formatter_schemas": {
            "BUSINESS": {
                "type": "object",
                "required": ["values"],
                "properties": {
                    "values": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["label", "amount"],
                            "properties": {
                                "label": {"type": "string"},
                                "amount": {"type": "number"},
                            },
                        },
                    }
                },
            }
        },
        "sql_tables": {
            "business_values": {
                "schema_keys": ["BUSINESS"],
                "records_path": ["values"],
                "columns": {
                    "label": {"type": "text", "source": ["label"], "nullable": False},
                    "amount": {
                        "type": "numeric",
                        "source": ["amount"],
                        "nullable": False,
                    },
                },
            }
        },
    }


def test_default_schema_file_matches_existing_relational_layout():
    assert load_bundle("schemas/default/bundle.json") == default_bundle()


def test_resume_uses_stored_configuration_and_schema_after_local_changes(engine):
    config = configured()
    bundle = default_bundle()
    runs.start_run(engine, run_id="first", config=config, bundle=bundle)
    first = runs.load_run_context(engine, "first", resume=True)
    config.max_image_retries = 19
    bundle["name"] = "new-description"
    runs.start_run(engine, run_id="second", config=config, bundle=bundle)
    second = runs.load_run_context(engine, "second")
    assert runs.load_run_context(engine, "first", resume=True) == first
    assert first["config"]["max_image_retries"] == 3
    assert second["config"]["max_image_retries"] == 19
    assert first["schema_id"] != second["schema_id"]
    runs.complete_run(engine, "first")
    with pytest.raises(ValueError, match="unfinished"):
        runs.load_run_context(engine, "first", resume=True)


def test_schema_registration_reuses_identical_content_and_detects_tampering(engine):
    for run in ["a", "b"]:
        runs.start_run(engine, run_id=run)
    assert count_rows(engine, tables.schema_versions) == 1
    with engine.begin() as connection:
        connection.execute(
            tables.schema_versions.update().values(definition={"changed": True})
        )
    with pytest.raises(ValueError, match="modified"):
        runs.load_run_context(engine, "a")


def test_custom_schema_flows_from_database_through_formatter_to_sql(engine):
    bundle = business_bundle()
    config = configured()
    config.fallback_schema = "BUSINESS"
    runs.start_run(engine, run_id="custom", config=config, bundle=bundle)
    client = fakeredis.FakeRedis()
    manifest = manifest_for(count=1)
    manifest.update(run_id="custom", schema_id=schema_id(bundle))
    client.set("pdf:1", json.dumps(manifest))
    save_vlm_result(client, "pdf:1", "pdf:1:image:0", "Revenue: 42.5", model="vision")
    formatter, create = formatter_for(
        model_response({"values": [{"label": "Revenue", "amount": 42.5}]})
    )
    format_image(
        client,
        formatter,
        manifest_key="pdf:1",
        image_key="pdf:1:image:0",
        database_engine=engine,
    )
    assert create.called
    assert client.zcard(DB_READY) == 1
    assert db.flush_ready(client, engine, final=True) == 1
    table = output_tables(bundle)["business_values"]
    with engine.connect() as connection:
        row = connection.execute(select(table)).mappings().one()
        assert row["label"] == "Revenue"
        assert float(row["amount"]) == 42.5
        assert connection.scalar(select(tables.images.c.failed)) is False
    assert load_manifest(client, "pdf:1")["db_status"] == "complete"


def test_writer_rejects_a_schema_substituted_after_formatting(engine):
    runs.start_run(engine, run_id="a")
    context = runs.load_run_context(engine, "a")
    manifest = complete_manifest(count=1)
    manifest.update(run_id="a", schema_id=context["schema_id"])
    formatting = manifest["assets"][0]["images_meta"][0]["formatting"]
    formatting.update(schema_id=context["schema_id"], schema_key="BAR_CHART")
    formatting["schema"] = {"type": "object"}
    with pytest.raises(ValueError, match="approved run schema"):
        db.write_batches(engine, [manifest])
    assert count_rows(engine, tables.documents) == 0


def test_omit_keeps_failed_identity_and_empty_details_in_whole_document_transaction(
    engine,
):
    config = configured()
    config.approve_with_fails = "omit"
    runs.start_run(engine, run_id="omit-run", config=config, bundle=default_bundle())
    context = runs.load_run_context(engine, "omit-run")
    client = fakeredis.FakeRedis()
    manifest = complete_manifest()
    manifest.update(
        run_id="omit-run", schema_id=context["schema_id"], approve_with_fails="omit"
    )
    good, bad = manifest["assets"][0]["images_meta"]
    good["formatting"].update(schema_id=context["schema_id"], schema_key="BAR_CHART")
    bad.update(
        vlm_status="failed",
        format_status="not_started",
        vlm_result=None,
        formatting=None,
    )
    client.set("pdf:1", json.dumps(manifest))
    update_manifest(client, "pdf:1", lambda value: None)
    assert client.zcard(DB_READY) == 0  # A retryable failure must not be stored yet.
    mark_image_failed(client, "pdf:1", bad["redis_key"])
    assert client.zcard(DB_READY) == 1
    assert db.flush_ready(client, engine, final=True) == 1
    assert count_rows(engine, tables.documents) == 1
    assert count_rows(engine, tables.images) == 2
    assert count_rows(engine, tables.charts) == 1
    with engine.connect() as connection:
        image = (
            connection.execute(
                select(tables.images).where(
                    tables.images.c.image_key == bad["redis_key"],
                )
            )
            .mappings()
            .one()
        )
        assert image["failed"] is True
        assert image["raw_output"] is None
        details = (
            connection.execute(
                select(tables.extraction_details).where(
                    tables.extraction_details.c.image_key == bad["redis_key"],
                )
            )
            .mappings()
            .one()
        )
        assert all(
            value is None for key, value in details.items() if key != "image_key"
        )
    saved = load_manifest(client, "pdf:1")
    assert saved["db_status"] == "complete"
    assert all("db_status" not in image for image in saved["assets"][0]["images_meta"])


def test_omit_cannot_override_a_runs_blocking_policy(engine):
    runs.start_run(engine, run_id="blocking")
    manifest = complete_manifest(count=1)
    manifest.update(run_id="blocking", approve_with_fails="omit")
    image = manifest["assets"][0]["images_meta"][0]
    image.update(failed=True, format_status="failed")
    with pytest.raises(ValueError, match="failure policy"):
        db.write_batches(engine, [manifest])
    assert count_rows(engine, tables.documents) == 0


def test_changing_custom_sql_creates_new_tables_without_reinterpreting_prior_runs(
    engine,
):
    config = configured()
    config.fallback_schema = "BUSINESS"
    old = business_bundle()
    new = deepcopy(old)
    new["sql_tables"]["business_values"]["columns"]["amount"]["description"] = (
        "Reported amount"
    )
    runs.start_run(engine, run_id="old", config=config, bundle=old)
    runs.start_run(engine, run_id="new", config=config, bundle=new)
    assert (
        output_tables(old)["business_values"].name
        != output_tables(new)["business_values"].name
    )
    assert runs.load_run_context(engine, "old")["schema"] == old
