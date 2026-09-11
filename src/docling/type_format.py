"""Define the slim manifest a single Docling pass produces."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

TEXT_POLICY = "verbatim_docling_text; no generated summary"
MATCHING_POLICY = "explicit figure/table labels; nearby does not imply relevance"


@dataclass
class ImageMeta:
    """Describe a Redis crop with its parent manifest and classification."""

    redis_key: str
    manifest_key: str
    page_number: int
    width: int
    height: int
    class_name: str | None = None
    confidence: float | None = None


@dataclass
class TextSnippet:
    """Hold one verbatim block of body text."""

    text: str


@dataclass
class AssetContext:
    """Keep the caption plus the surrounding verbatim text."""

    caption: str
    mentions: list[TextSnippet] = field(default_factory=list)
    nearby: list[TextSnippet] = field(default_factory=list)
    text_policy: str = TEXT_POLICY
    matching_policy: str = MATCHING_POLICY


@dataclass
class Asset:
    """Describe one extracted figure or table and its saved crops."""

    asset_id: str
    name: str
    images_meta: list[ImageMeta] = field(default_factory=list)
    context: AssetContext | None = None
    status: Literal["completed", "failed_processing"] | None = None
    error: str | None = None


@dataclass
class DiscardedPicture:
    """Record a picture excluded before any crop was saved."""

    item_ref: str
    reason: str
    classification: dict[str, Any] | None = None


@dataclass
class DocumentMetadata:
    """Hold conservatively extracted citation fields and their evidence."""

    title: str | None = None
    authors: list[str] = field(default_factory=list)
    publication_year: int | None = None
    journal: str | None = None
    doi: str | None = None
    arxiv_id: str | None = None
    abstract: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


@dataclass
class Manifest:
    """Describe one PDF pass, its assets, and its document-level bookkeeping."""

    schema_version: Literal[2]
    document_id: str | None
    pdf_sha256: str | None
    source_path: str
    name: str
    started_at: str
    status: Literal[
        "processing",
        "completed",
        "no_assets",
        "partial_failure",
        "failed_processing",
    ]
    manifest_key: str
    assets: list[Asset] = field(default_factory=list)
    below_threshold: list[Asset] = field(default_factory=list)
    discarded: list[DiscardedPicture] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    docling_version: str | None = None
    conversion_status: str | None = None
    document_metadata: DocumentMetadata | None = None
    page_count: int | None = None
    finished_at: str | None = None
