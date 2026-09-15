"""Validate the proposed setup and render SQL without connecting to services."""

import importlib.util
import json
import sys
from pathlib import Path

from jsonschema import Draft202012Validator
from parser.src.db.schemas import load_bundle, output_tables, record_rows, schema_id
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable


def main():
    folder = Path(__file__).resolve().parent
    spec = importlib.util.spec_from_file_location("proposed_chart_config", folder / "config.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    bundle = load_bundle(folder / "bundle.json")
    snapshot = module.CONFIG.snapshot(bundle["formatter_schemas"])
    example = json.loads((folder / "example-output.json").read_text())
    Draft202012Validator(bundle["formatter_schemas"]["MEASUREMENTS"]).validate(example["data"])
    rows = record_rows(bundle, "MEASUREMENTS", example["data"], "example-image-key")
    (folder / "example-sql-rows.json").write_text(json.dumps(rows, indent=2) + "\n")
    dialect = postgresql.dialect()
    ddl = "\n\n".join(str(CreateTable(table).compile(dialect=dialect)) for table in output_tables(bundle).values())
    (folder / "schema.sql").write_text("-- Proposed layout only; not executed. Shared images table must already exist.\n" + ddl + ";\n")
    result = {
        "configuration_valid": True,
        "bundle_valid": True,
        "example_valid": True,
        "example_rows": sum(map(len, rows.values())),
        "schema_id": schema_id(bundle),
        "models": {key: model["model_id"] for key, model in snapshot["models"].items()},
        "inference_tested": False,
        "database_connected": False,
        "active_configuration_changed": False,
    }
    (folder / "validation.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
