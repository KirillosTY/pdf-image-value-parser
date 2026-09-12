"""Persist complete PDFs atomically, with at most 50 PDFs per transaction."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from copy import deepcopy
from itertools import islice
from uuid import NAMESPACE_URL, uuid5

from jsonschema import Draft202012Validator
from parser.src.db import tables
from parser.src.redis.state import (
    DB_READY,
    document_ready,
    load_manifest,
    manifest_fingerprint,
    mark_document_stored,
)
from redis.exceptions import LockNotOwnedError
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from schema_format import CHART_SCHEMAS


def document_rows(manifest: dict) -> dict[str, list[dict]]:
    """Map every image in a complete document into relational rows."""
    if not document_ready(manifest):
        raise ValueError("Every image must finish VLM and formatting before DB writing")
    manifest_key = manifest["manifest_key"]
    metadata = manifest.get("document_metadata") or {}
    rows = defaultdict(list)
    rows["documents"].append(
        {
            "manifest_key": manifest_key,
            "document_id": manifest.get("document_id"),
            "run_id": manifest.get("run_id"),
            "pdf_sha256": manifest.get("pdf_sha256"),
            "source_path": manifest["source_path"],
            "title": metadata.get("title"),
            "publication_year": metadata.get("publication_year"),
            "doi": metadata.get("doi"),
            "metadata": metadata,
            "snapshot_hash": manifest_fingerprint(manifest),
        }
    )
    seen_images = set()
    for asset in manifest["assets"]:
        for image in asset.get("images_meta", []):
            image_key = image["redis_key"]
            if image_key in seen_images or image["manifest_key"] != manifest_key:
                raise ValueError(
                    "Duplicate image or image belonging to a different manifest"
                )
            seen_images.add(image_key)
            vlm = image["vlm_result"]
            formatting = image["formatting"]
            if not isinstance(vlm["raw_output"], str) or not vlm["raw_output"].strip():
                raise ValueError("Saved VLM text is required")
            Draft202012Validator.check_schema(formatting["schema"])
            Draft202012Validator(formatting["schema"]).validate(formatting["data"])
            rows["images"].append(
                {
                    "image_key": image_key,
                    "manifest_key": manifest_key,
                    "asset_id": asset["asset_id"],
                    "page_number": image["page_number"],
                    "width": image["width"],
                    "height": image["height"],
                    "raw_output": vlm["raw_output"],
                    "vlm_model": vlm.get("model"),
                    "context": asset.get("context"),
                }
            )
            rows["extraction_details"].append(
                {
                    "image_key": image_key,
                    "formatter_model": formatting["model"],
                    "schema": formatting["schema"],
                    "model_response": formatting["model_response"],
                    "formatted_data": formatting["data"],
                    "unmapped_observations": formatting["unmapped_observations"],
                }
            )
            data = formatting["data"]
            # A caller can supply a predefined multi-panel schema containing a
            # charts array. The existing single-chart schemas still work as-is.
            chart_data = data.get("charts") if "charts" in data else [data]
            if not isinstance(chart_data, list) or not chart_data:
                raise ValueError(
                    "Formatted image must contain at least one chart record"
                )
            for ordinal, chart in enumerate(chart_data):
                kind = chart.get("chart_type")
                if kind not in CHART_SCHEMAS:
                    raise ValueError(f"No relational mapping for chart type: {kind}")
                Draft202012Validator(CHART_SCHEMAS[kind]).validate(chart)
                chart_id = uuid5(NAMESPACE_URL, f"{image_key}:chart:{ordinal}").hex
                chart_row = {
                    column.name: chart.get(column.name) for column in tables.charts.c
                }
                chart_row.update(
                    chart_id=chart_id, image_key=image_key, chart_type=kind
                )
                rows["charts"].append(chart_row)
                if kind in tables.MEASUREMENT_TABLES:
                    _measurement_rows(
                        rows, tables.MEASUREMENT_TABLES[kind], chart_id, chart["data"]
                    )
                if kind == "FLOW_CHART":
                    _measurement_rows(rows, tables.flow_nodes, chart_id, chart["nodes"])
                    _measurement_rows(rows, tables.flow_edges, chart_id, chart["edges"])
                elif kind == "COMBO_CHART":
                    _measurement_rows(
                        rows, tables.combo_series, chart_id, chart["series_types"]
                    )
                elif kind == "UNKNOWN":
                    _measurement_rows(
                        rows,
                        tables.unknown_observations,
                        chart_id,
                        [{"observation": text} for text in chart["observations"]],
                    )
    return dict(rows)


def _measurement_rows(rows, table, chart_id: str, observations: list[dict]) -> None:
    for ordinal, observation in enumerate(observations):
        row = {column.name: observation.get(column.name) for column in table.c}
        row.update(chart_id=chart_id, ordinal=ordinal)
        rows[table.name].append(row)


def _write_transaction(engine, batch: list[dict]) -> None:
    """Commit all document, image, chart, and measurement rows together."""
    rows = defaultdict(list)
    for manifest in batch:
        for table_name, records in document_rows(manifest).items():
            rows[table_name].extend(records)
    with engine.begin() as connection:
        for table in tables.metadata.sorted_tables:
            records = rows.get(table.name, [])
            # Bound SQL statement sizes without splitting the transaction/PDF.
            for offset in range(0, len(records), 500):
                statement = insert(table).values(records[offset : offset + 500])
                statement = statement.on_conflict_do_nothing(
                    index_elements=list(table.primary_key.columns),
                )
                connection.execute(statement)
            if table is tables.documents:
                for document in records:
                    stored_hash = connection.execute(
                        select(tables.documents.c.snapshot_hash).where(
                            tables.documents.c.manifest_key == document["manifest_key"]
                        )
                    ).scalar_one()
                    if stored_hash != document["snapshot_hash"]:
                        raise ValueError(
                            "This PDF attempt was already stored with different content"
                        )


def write_batches(
    engine, manifests: Iterable[dict], *, batch_size: int = 50, on_committed=None
) -> int:
    """Write a finite iterable of complete PDFs, including its smaller tail."""
    if type(batch_size) is not int or batch_size < 1:
        raise ValueError("batch_size must be a positive integer")
    iterator = iter(manifests)
    written = 0
    while incoming := list(islice(iterator, batch_size)):
        batch = deepcopy(incoming)
        keys = [manifest["manifest_key"] for manifest in batch]
        if len(set(keys)) != len(keys):
            raise ValueError("A batch cannot contain the same PDF attempt twice")
        _write_transaction(engine, batch)
        if on_committed is not None:
            on_committed(batch)
        written += len(batch)
    return written


def flush_ready(client, engine, *, batch_size: int = 50, final: bool = False) -> int:
    """Write one ready batch; flush a smaller tail only when final is true."""
    if type(batch_size) is not int or batch_size < 1:
        raise ValueError("batch_size must be a positive integer")
    lock = client.lock("documents:db_writer_lock", timeout=600, blocking=False)
    if not lock.acquire():
        return 0
    try:
        keys = client.zrange(DB_READY, 0, batch_size - 1)
        if not keys or (len(keys) < batch_size and not final):
            return 0
        manifests = [load_manifest(client, key) for key in keys]

        def acknowledge(batch):
            for manifest in batch:
                mark_document_stored(client, manifest)

        return write_batches(
            engine, manifests, batch_size=batch_size, on_committed=acknowledge
        )
    finally:
        try:
            lock.release()
        except LockNotOwnedError:
            pass
