"""Track runs and restrict document recovery lookups to unfinished runs."""

from uuid import uuid4

from parser.src.db.schemas import (
    default_bundle,
    load_bundle,
    load_schema,
    register_schema,
)
from parser.src.db.tables import documents, run_info
from sqlalchemy import func, select

from config import PipelineConfig


def start_run(
    engine,
    *,
    run_id: str | None = None,
    config: PipelineConfig | None = None,
    bundle: dict | None = None,
) -> str:
    """Register a new batch; existing run IDs must be resumed explicitly."""
    identifier = run_id or uuid4().hex
    if config is not None:
        bundle = bundle if bundle is not None else load_bundle(config.schema_bundle)
        snapshot = config.snapshot(bundle["formatter_schemas"])
    else:
        # Preserve the standalone extraction API until model setup is supplied.
        if bundle is not None and bundle.get("mapping") != "charts-v1":
            raise ValueError("Custom schema bundles require an approved PipelineConfig")
        bundle = bundle if bundle is not None else default_bundle()
        snapshot = {
            "approve_with_fails": False,
            "schema_matcher": {},
            "fallback_schema": "UNKNOWN",
        }
    with engine.begin() as connection:
        version = register_schema(connection, bundle)
        connection.execute(
            run_info.insert().values(
                run_id=identifier,
                status="incomplete",
                schema_id=version,
                config_snapshot=snapshot,
            )
        )
    return identifier


def load_run_context(engine, run_id: str, *, resume: bool = False) -> dict:
    """Recover the run's approved configuration and schema through one DB lookup."""
    with engine.connect() as connection:
        row = (
            connection.execute(select(run_info).where(run_info.c.run_id == run_id))
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise ValueError(f"Run not found: {run_id}")
        if resume and row["status"] != "incomplete":
            raise ValueError(f"Run {run_id!r} is not an unfinished run")
        if not row["schema_id"] or row["config_snapshot"] is None:
            raise ValueError(
                "Legacy run has no approved schema snapshot; explicitly migrate it first"
            )
        return {
            "run_id": run_id,
            "status": row["status"],
            "schema_id": row["schema_id"],
            "config": row["config_snapshot"],
            "schema": load_schema(connection, row["schema_id"]),
        }


def require_incomplete_run(engine, run_id: str) -> None:
    """Reject unknown or completed runs before starting or resuming workers."""
    with engine.connect() as connection:
        status = connection.scalar(
            select(run_info.c.status).where(run_info.c.run_id == run_id)
        )
    if status != "incomplete":
        raise ValueError(f"Run {run_id!r} is not an unfinished run")


def document_is_stored_for_resume(engine, run_id: str, document_id: str) -> bool:
    """Find a committed metadata identity only within this unfinished run."""
    statement = select(
        select(documents.c.manifest_key)
        .join(run_info, documents.c.run_id == run_info.c.run_id)
        .where(
            run_info.c.run_id == run_id,
            run_info.c.status == "incomplete",
            documents.c.document_id == document_id,
        )
        .exists()
    )
    with engine.connect() as connection:
        return bool(connection.scalar(statement))


def complete_run(engine, run_id: str) -> None:
    """Mark success only when the caller has drained the entire pipeline."""
    with engine.begin() as connection:
        result = connection.execute(
            run_info.update()
            .where(run_info.c.run_id == run_id, run_info.c.status == "incomplete")
            .values(status="complete", completed_at=func.now())
        )
        if result.rowcount != 1:
            raise ValueError(f"Run {run_id!r} is not an unfinished run")


def finish_run(engine, run_id: str, *, outcome: str, errors: list[str]) -> None:
    """Record a terminal outcome, leaving fatal runs eligible for recovery."""
    if outcome not in {"completed", "completed_with_errors", "failed"}:
        raise ValueError("Invalid run outcome")
    with engine.begin() as connection:
        result = connection.execute(run_info.update().where(run_info.c.run_id == run_id).values(
            status="incomplete" if outcome == "failed" else "complete",
            outcome=outcome, errors=list(errors), completed_at=func.now(),
        ))
        if result.rowcount != 1:
            raise ValueError(f"Run not found: {run_id}")
