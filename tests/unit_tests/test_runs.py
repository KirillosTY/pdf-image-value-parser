from parser.src.db import runs, tables
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles


@compiles(JSONB, "sqlite")
def compile_jsonb_for_sqlite(type_, compiler, **kwargs):
    """Use SQLite JSON when exercising run recovery locally."""
    return "JSON"


def test_resume_lookup_is_scoped_to_incomplete_run():
    engine = create_engine("sqlite://")
    tables.metadata.create_all(engine)
    run_id = runs.start_run(engine, run_id="run-a")
    with engine.begin() as connection:
        connection.execute(
            tables.documents.insert().values(
                manifest_key="manifest-a",
                document_id="doc-a",
                run_id=run_id,
                pdf_sha256="bytes-a",
                source_path="paper.pdf",
                metadata={},
                snapshot_hash="snapshot-a",
            )
        )
    assert runs.document_is_stored_for_resume(engine, "run-a", "doc-a")
    runs.complete_run(engine, "run-a")
    assert not runs.document_is_stored_for_resume(engine, "run-a", "doc-a")
    assert not runs.document_is_stored_for_resume(engine, "run-b", "doc-a")


def test_new_run_with_same_document_identity_is_independent():
    engine = create_engine("sqlite://")
    tables.metadata.create_all(engine)
    runs.start_run(engine, run_id="run-a")
    runs.start_run(engine, run_id="run-b")
    with engine.begin() as connection:
        connection.execute(
            tables.documents.insert().values(
                manifest_key="manifest-a",
                document_id="same-doc",
                run_id="run-a",
                source_path="a.pdf",
                metadata={},
                snapshot_hash="a",
            )
        )
    assert not runs.document_is_stored_for_resume(engine, "run-b", "same-doc")
