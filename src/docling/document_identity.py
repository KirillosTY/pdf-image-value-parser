"""Build document identities from extracted metadata."""

import hashlib
import json
from pathlib import Path

from parser.src.docling.type_format import DocumentMetadata


def normalize(value: str | int | None) -> str:
    """Normalize capitalization and whitespace."""
    if value is None:
        return ""
    return " ".join(str(value).split()).casefold()


def identity_metadata(metadata: DocumentMetadata) -> list[object]:
    """Return normalized identity fields in their fixed order."""
    doi = normalize(metadata.doi)
    title = normalize(metadata.title)
    authors = [normalized for author in metadata.authors
               if (normalized := normalize(author))]
    organization = normalize(metadata.organization)
    date = normalize(metadata.publication_date) or normalize(metadata.publication_year)
    return [doi, title, authors, organization, date]


def make_document_id(
    metadata: DocumentMetadata, *, run_id: str, filename: str
) -> str:
    """Hash metadata in fixed order, or the run ID and filename if all absent."""
    doi, title, authors, organization, date = identity_metadata(metadata)
    if doi or title or authors or organization or date:
        identity = ["metadata", doi, title, authors, organization, date]
    else:
        if not run_id:
            raise ValueError("run_id is required for fallback identity")
        identity = ["fallback", run_id, Path(filename).name]
    serialized = json.dumps(identity, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()
