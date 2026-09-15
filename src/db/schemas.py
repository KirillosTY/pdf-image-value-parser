"""Version approved formatter schemas and their SQL mappings in Postgres."""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from pathlib import Path

from jsonschema import Draft202012Validator
from parser.src.db import tables
from sqlalchemy import (
    Boolean,
    Column,
    ForeignKey,
    Integer,
    MetaData,
    Numeric,
    Table,
    Text,
    select,
)
from sqlalchemy.dialects.postgresql import JSONB, insert

from schema_format import CHART_SCHEMAS

SQL_TYPES = {
    "text": Text,
    "numeric": Numeric,
    "integer": Integer,
    "boolean": Boolean,
    "json": JSONB(none_as_null=True),
}
IDENTIFIER = re.compile(r"[a-z][a-z0-9_]{0,31}\Z")


def default_bundle() -> dict:
    """Describe the existing chart layout as the initial approved schema version."""
    shared = {"schema_versions", "run_info", "documents", "images"}
    return {
        "name": "charts-v1",
        "mapping": "charts-v1",
        "formatter_schemas": deepcopy(CHART_SCHEMAS),
        "sql_tables": {
            table.name: {
                column.name: {
                    "type": str(column.type),
                    "nullable": column.nullable,
                    "primary_key": column.primary_key,
                    "references": sorted(
                        fk.target_fullname for fk in column.foreign_keys
                    ),
                }
                for column in table.c
            }
            for table in tables.metadata.sorted_tables
            if table.name not in shared
        },
    }


def schema_id(bundle: dict) -> str:
    """Identify the entire definition, including column mappings and descriptions."""
    payload = json.dumps(bundle, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(payload.encode()).hexdigest()


def _identifier(value) -> None:
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise ValueError(
            f"Use a lowercase SQL identifier of at most 32 characters: {value!r}"
        )


def _path(value) -> None:
    if not isinstance(value, list) or not all(isinstance(key, str) for key in value):
        raise ValueError("A source path must be a list of object property names")


def _schema_path(schema: dict, path: list[str]) -> dict:
    for key in path:
        if schema.get("type") != "object" or key not in schema.get("properties", {}):
            raise ValueError(
                f"Source path is not declared in the formatter schema: {path}"
            )
        schema = schema["properties"][key]
    return schema


def validate_bundle(bundle: dict) -> None:
    """Validate schemas and declarative SQL before registering a version."""
    schema_id(bundle)  # Reject values that cannot be snapshotted as JSON.
    if not isinstance(bundle.get("name"), str) or not bundle["name"].strip():
        raise ValueError("A schema bundle needs a name")
    schemas = bundle.get("formatter_schemas")
    if not isinstance(schemas, dict) or not schemas:
        raise ValueError("A bundle needs formatter_schemas")
    for schema in schemas.values():
        Draft202012Validator.check_schema(schema)
        if schema.get("type") != "object":
            raise ValueError("Formatter output schemas must describe an object")
    if bundle.get("mapping") == "charts-v1":
        default = default_bundle()
        if (
            bundle["sql_tables"] != default["sql_tables"]
            or schemas != default["formatter_schemas"]
        ):
            raise ValueError(
                "Changed chart layouts need a new mapping; use 'records-v1' for custom tables"
            )
        return
    if bundle.get("mapping") != "records-v1":
        raise ValueError("Supported mappings are charts-v1 and records-v1")
    definitions = bundle.get("sql_tables")
    if not isinstance(definitions, dict) or not definitions:
        raise ValueError("A records bundle needs sql_tables")
    covered = set()
    for name, definition in definitions.items():
        _identifier(name)
        if set(definition) - {"schema_keys", "records_path", "columns", "description"}:
            raise ValueError(f"Unsupported table options: {name}")
        keys = definition.get("schema_keys", [])
        if not keys or set(keys) - schemas.keys():
            raise ValueError(f"Select existing schema_keys for table {name}")
        covered.update(keys)
        _path(definition.get("records_path"))
        columns = definition.get("columns")
        if not isinstance(columns, dict) or not columns:
            raise ValueError(f"Define columns for table {name}")
        for column, spec in columns.items():
            _identifier(column)
            if set(spec) - {"type", "source", "nullable", "description", "unit"}:
                raise ValueError(f"Unsupported column options: {name}.{column}")
            if column in {"image_key", "ordinal"} or spec.get("type") not in SQL_TYPES:
                raise ValueError(f"Reserved column or unsupported SQL type: {column}")
            _path(spec.get("source"))
            if type(spec.get("nullable", True)) is not bool:
                raise ValueError("nullable must be a boolean")
        for key in keys:
            record = _schema_path(schemas[key], definition["records_path"])
            if record.get("type") == "array":
                record = record["items"]
            elif record.get("type") != "object":
                raise ValueError("records_path must select an object or array schema")
            for column, spec in columns.items():
                source = _schema_path(record, spec["source"])
                compatible = {
                    "text": {"string"},
                    "numeric": {"number", "integer"},
                    "integer": {"integer"},
                    "boolean": {"boolean"},
                    "json": {
                        "object",
                        "array",
                        "string",
                        "number",
                        "integer",
                        "boolean",
                        "null",
                    },
                }
                kinds = source.get("type")
                kinds = set(kinds) if isinstance(kinds, list) else {kinds}
                if spec.get("nullable", True):
                    kinds.discard("null")
                if not kinds <= compatible[spec["type"]]:
                    raise ValueError(
                        f"SQL type does not match formatter field: {name}.{column}"
                    )
    if covered != set(schemas):
        raise ValueError("Every formatter schema needs a SQL table mapping")


def load_bundle(path: str | Path) -> dict:
    """Read an approved JSON bundle from the setup folder."""
    bundle = json.loads(Path(path).read_text())
    validate_bundle(bundle)
    return bundle


def output_tables(bundle: dict) -> dict[str, Table]:
    """Build isolated, versioned tables linked to shared image provenance."""
    if bundle["mapping"] != "records-v1":
        return {}
    metadata = MetaData()
    tables.images.to_metadata(metadata)
    # Only the image primary key is needed to resolve extracted-table FKs.
    prefix = f"extracted_{schema_id(bundle)[:16]}_"
    return {
        name: Table(
            prefix + name,
            metadata,
            Column("image_key", Text, ForeignKey("images.image_key"), primary_key=True),
            Column("ordinal", Integer, primary_key=True),
            *(
                Column(
                    column,
                    SQL_TYPES[spec["type"]],
                    nullable=spec.get("nullable", True),
                    comment=spec.get("description"),
                )
                for column, spec in definition["columns"].items()
            ),
        )
        for name, definition in bundle["sql_tables"].items()
    }


def register_schema(connection, bundle: dict) -> str:
    """Insert an immutable definition and create its output tables in this transaction."""
    validate_bundle(bundle)
    identifier = schema_id(bundle)
    connection.execute(
        insert(tables.schema_versions)
        .values(
            schema_id=identifier,
            name=bundle["name"],
            definition=bundle,
        )
        .on_conflict_do_nothing(index_elements=[tables.schema_versions.c.schema_id])
    )
    saved = load_schema(connection, identifier)
    if saved != bundle:
        raise ValueError("Schema identity collision")
    for table in output_tables(bundle).values():
        table.create(connection, checkfirst=True)
    return identifier


def load_schema(connection, identifier: str) -> dict:
    """Read the stored definition and detect modifications to a version."""
    bundle = connection.scalar(
        select(tables.schema_versions.c.definition).where(
            tables.schema_versions.c.schema_id == identifier,
        )
    )
    if bundle is None:
        raise ValueError(f"Schema version not found: {identifier}")
    if schema_id(bundle) != identifier:
        raise ValueError("Stored schema version has been modified")
    return deepcopy(bundle)


def read_path(value, path: list[str]):
    """Read nested object fields without executing expressions."""
    for key in path:
        if not isinstance(value, dict) or key not in value:
            raise ValueError(f"Missing mapped source path: {path}")
        value = value[key]
    return value


def record_rows(
    bundle: dict, key: str, data: dict, image_key: str
) -> dict[str, list[dict]]:
    """Map validated formatter output to versioned SQL rows."""
    result = {}
    physical = output_tables(bundle)
    for name, definition in bundle["sql_tables"].items():
        if key not in definition["schema_keys"]:
            continue
        records = read_path(data, definition["records_path"])
        if isinstance(records, dict):
            records = [records]
        if not isinstance(records, list):
            raise ValueError("records_path must select an object or an array")
        rows = []
        for ordinal, record in enumerate(records):
            row = {"image_key": image_key, "ordinal": ordinal}
            for column, spec in definition["columns"].items():
                try:
                    value = read_path(record, spec["source"])
                except ValueError:
                    if not spec.get("nullable", True):
                        raise
                    value = None
                if value is None and not spec.get("nullable", True):
                    raise ValueError(f"Required SQL value is missing: {name}.{column}")
                row[column] = value
            rows.append(row)
        result[physical[name].name] = rows
    return result
