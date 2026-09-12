"""Track runs and restrict document recovery lookups to unfinished runs."""

from uuid import uuid4

from parser.src.db.tables import documents, run_info
from sqlalchemy import func, select


def start_run(engine, *, run_id: str | None = None) -> str:
    """Register a new batch; existing run IDs must be resumed explicitly."""
    identifier = run_id or uuid4().hex
    with engine.begin() as connection:
        connection.execute(run_info.insert().values(run_id=identifier, status="incomplete"))
    return identifier


def require_incomplete_run(engine, run_id: str) -> None:
    """Reject unknown or completed runs before starting or resuming workers."""
    with engine.connect() as connection:
        status = connection.scalar(select(run_info.c.status).where(run_info.c.run_id == run_id))
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
        result = connection.execute(run_info.update().where(
            run_info.c.run_id == run_id, run_info.c.status == "incomplete"
        ).values(status="complete", completed_at=func.now()))
        if result.rowcount != 1:
            raise ValueError(f"Run {run_id!r} is not an unfinished run")
