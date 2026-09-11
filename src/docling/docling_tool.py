"""Read a PDF and emit one slim manifest of its chart/table figures."""

from __future__ import annotations

import hashlib
import importlib.metadata
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from figure_parser.document_text import (
    context_for,
    document_metadata,
    figure_label,
    find_captions,
    provenance,
    text_blocks,
    value,
)
from figure_parser.type_format import (
    Asset,
    AssetContext,
    DocumentMetadata,
    ImageMeta,
    Manifest,
    TextSnippet,
)
from parser.src.redis.state import create_manifest_key, queue_manifest, store_image

LOG = logging.getLogger(__name__)
DEFAULT_IMAGES = Path(__file__).resolve().parents[2] / "images"
CONVERSION_OK = {"success", "partial_success"}
CHART_CLASSES = {
    "line_chart", "bar_chart", "pie_chart", "scatter_plot", "box_plot", "table",
}


@dataclass(frozen=True)
class ExtractionConfig:
    """Configure the device, crop resolution, and picture selection."""

    device: str = "auto"
    image_dpi: int = 216
    batch_size: int = 2
    classification_threshold: float = 0.80
    nearby_paragraphs: int = 2


@dataclass
class ExtractionContext:
    """Bundle the per-document values every asset builder needs."""

    document: Any
    blocks: list[dict[str, Any]]
    document_id: str | None
    manifest_key: str
    config: ExtractionConfig


def sha256_of_file(path: Path) -> str:
    """Identify a document by its bytes across filenames and runs."""
    running = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            running.update(chunk)
    return running.hexdigest()


def minute_timestamp() -> str:
    """Return a minute-precision UTC timestamp."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M")


def top_classification(picture: Any) -> dict[str, Any] | None:
    """Return the highest-confidence predicted class for a picture."""
    prediction = getattr(getattr(picture, "meta", None), "classification", None)
    predictions = (
        [p.model_dump(mode="json") for p in prediction.predictions]
        if prediction
        else []
    )
    best = max(predictions, key=lambda p: p["confidence"], default=None)
    return (
        {"class_name": best["class_name"], "confidence": best["confidence"]}
        if best
        else None
    )


def is_chart(classification: dict[str, Any] | None) -> bool:
    """Report whether a classification names one of the chart classes."""
    return bool(classification) and classification["class_name"] in CHART_CLASSES


def asset_id_for(document_id: str | None, item_ref: str) -> str:
    """Derive a stable asset id from the document and item reference."""
    return hashlib.sha256(f"{document_id}:{item_ref}".encode()).hexdigest()


def asset_name(kind: str, label: tuple[str, str] | None) -> str:
    """Name the asset from its printed label, e.g. ``figure 2``."""
    return f"{kind} {label[1]}".lower() if label else kind


def new_manifest(pdf_path: Path, document_id: str | None) -> Manifest:
    """Start a manifest header before extraction fills it in."""
    return Manifest(
        schema_version=2,
        document_id=document_id,
        pdf_sha256=document_id,
        source_path=str(pdf_path),
        name=pdf_path.stem,
        started_at=minute_timestamp(),
        status="processing",
        manifest_key=create_manifest_key(document_id or "unreadable"),
    )


def read_document_metadata(pdf_path: Path, blocks: list[dict[str, Any]]) -> DocumentMetadata:
    """Extract citation metadata, dropping the internal front matter."""
    fields = document_metadata(pdf_path, blocks)
    fields.pop("front_matter", None)
    return DocumentMetadata(**fields)


def caption_records(captions: list[Any]) -> list[dict[str, Any]]:
    """Shape resolved captions into the records ``context_for`` expects."""
    return [
        {"text": caption.text, "item_ref": caption.self_ref, "provenance": provenance(caption)}
        for caption in captions
    ]


def to_asset_context(raw_context: dict[str, Any]) -> AssetContext:
    """Keep only the caption and verbatim mention/nearby text."""
    return AssetContext(
        caption="\n".join(caption["text"] for caption in raw_context["captions"]),
        mentions=[TextSnippet(mention["text"]) for mention in raw_context["mentions"]],
        nearby=[TextSnippet(block["text"]) for block in raw_context["nearby"]],
        text_policy=raw_context["text_policy"],
        matching_policy=raw_context["matching_policy"],
    )


def save_crops(
    context: ExtractionContext,
    item: Any,
    asset_id: str,
    classification: dict[str, Any] | None,
) -> list[ImageMeta]:
    """Store each crop in Redis and return its image record."""
    class_name = classification["class_name"] if classification else None
    confidence = classification["confidence"] if classification else None
    images: list[ImageMeta] = []
    for ordinal, page in enumerate(item.prov, start=1):
        page_number = page.page_no
        image = item.get_image(context.document, prov_index=ordinal - 1)
        if image is None:
            raise ValueError(f"No crop returned for page {page_number}")
        try:
            image_key = store_image(
                image, context.manifest_key, asset_id, ordinal
            )
            images.append(
                ImageMeta(
                    redis_key=image_key,
                    manifest_key=context.manifest_key,
                    page_number=page_number,
                    width=image.width,
                    height=image.height,
                    class_name=class_name,
                    confidence=confidence,
                )
            )
        finally:
            image.close()
    return images


def build_asset(
    context: ExtractionContext,
    item: Any,
    kind: str,
    classification: dict[str, Any] | None,
    order: int,
) -> Asset:
    """Assemble one asset, saving its crops and trimming its context."""
    captions = find_captions(context.document, item, kind, set())
    label = figure_label("\n".join(caption.text for caption in captions))
    asset_id = asset_id_for(context.document_id, item.self_ref)
    pages = {page.page_no for page in item.prov}
    asset = Asset(
        asset_id=asset_id,
        name=asset_name(kind, label),
        context=to_asset_context(
            context_for(
                context.blocks,
                caption_records(captions),
                label,
                pages,
                order,
                context.config.nearby_paragraphs,
            )
        ),
    )
    try:
        if not pages:
            raise ValueError("Missing page provenance")
        asset.images_meta = save_crops(context, item, asset_id, classification)
        asset.status = "completed"
    except Exception as exc:
        asset.status = "failed_processing"
        asset.error = f"{type(exc).__name__}: {exc}"
        LOG.exception("Failed processing asset %s", asset_id)
    return asset


def final_status(manifest: Manifest, conversion_status: str) -> str:
    """Decide the manifest status from its errors and assets."""
    if manifest.errors or conversion_status == "partial_success":
        return "partial_failure"
    return "completed" if manifest.assets else "no_assets"


class DocumentWorker:
    """Convert PDFs to slim manifests, reusing one Docling converter."""

    def __init__(
        self,
        output_dir: Path | str = DEFAULT_IMAGES,
        config: ExtractionConfig | None = None,
    ):
        """Validate the configuration and defer Docling until first use."""
        self.output_dir = Path(output_dir).expanduser().resolve()
        self.config = config or ExtractionConfig()
        if self.config.device not in {"auto", "cpu", "cuda", "mps"}:
            raise ValueError("device must be auto, cpu, cuda, or mps")
        if min(self.config.image_dpi, self.config.batch_size) <= 0:
            raise ValueError("DPI and batch size must be positive")
        if not 0 <= self.config.classification_threshold <= 1:
            raise ValueError("classification_threshold must be between 0 and 1")
        self._converter: Any = None

    def _get_converter(self) -> Any:
        if self._converter is None:
            from docling.datamodel.accelerator_options import AcceleratorOptions
            from docling.datamodel.base_models import InputFormat
            from docling.datamodel.pipeline_options import PdfPipelineOptions
            from docling.document_converter import DocumentConverter, PdfFormatOption

            options = PdfPipelineOptions(
                do_ocr=False,
                do_table_structure=False,
                do_picture_classification=True,
                do_picture_description=False,
                generate_page_images=True,
                images_scale=self.config.image_dpi / 72,
                layout_batch_size=self.config.batch_size,
                queue_max_size=8,
                accelerator_options=AcceleratorOptions(device=self.config.device),
            )
            options.picture_classification_options.engine_options.top_k = 1
            self._converter = DocumentConverter(
                format_options={
                    InputFormat.PDF: PdfFormatOption(pipeline_options=options)
                },
            )
        return self._converter

    def _classify_item(self, item: Any) -> tuple[str, dict[str, Any] | None] | None:
        from docling_core.types.doc.items.picture.picture import PictureItem
        from docling_core.types.doc.items.table.table import TableItem

        if isinstance(item, TableItem):
            return "table", None
        if isinstance(item, PictureItem):
            classification = top_classification(item)
            return ("figure", classification) if is_chart(classification) else None
        return None

    def _extract(self, pdf_path: Path, manifest: Manifest) -> None:
        result = self._get_converter().convert(pdf_path, raises_on_error=False)
        conversion_status = value(result.status)
        manifest.conversion_status = conversion_status
        manifest.errors.extend(str(error) for error in result.errors)
        if conversion_status not in CONVERSION_OK:
            raise RuntimeError(f"Docling conversion status: {conversion_status}")

        document = result.document
        context = ExtractionContext(
            document=document,
            blocks=text_blocks(document),
            document_id=manifest.document_id,
            manifest_key=manifest.manifest_key,
            config=self.config,
        )
        manifest.document_metadata = read_document_metadata(pdf_path, context.blocks)
        manifest.page_count = len(document.pages)

        for order, (item, _) in enumerate(document.iterate_items()):
            classified = self._classify_item(item)
            if classified is None:
                continue
            kind, classification = classified
            asset = build_asset(context, item, kind, classification, order)
            if asset.error:
                manifest.errors.append(asset.error)
            manifest.assets.append(asset)

        manifest.status = final_status(manifest, conversion_status)

    def process_pdf(self, source: Path | str) -> Manifest:
        """Read one PDF and queue its completed manifest in Redis."""
        pdf_path = Path(source).expanduser().resolve()
        try:
            document_id = sha256_of_file(pdf_path)
        except OSError:
            document_id = None
        manifest = new_manifest(pdf_path, document_id)
        try:
            if document_id is None:
                raise OSError(f"Could not read {pdf_path}")
            manifest.docling_version = importlib.metadata.version("docling")
            self._extract(pdf_path, manifest)
        except Exception as exc:
            manifest.status = "failed_processing"
            manifest.errors.append(f"{type(exc).__name__}: {exc}")
            LOG.exception("Failed processing %s: %s", pdf_path, exc)
        manifest.finished_at = datetime.now(timezone.utc).isoformat()
        if manifest.status == "completed":
            queue_manifest(manifest, manifest.manifest_key)
        return manifest

    def process_folder(
        self, source: Path | str, *, recursive: bool = False
    ) -> list[Manifest]:
        """Extract every PDF under a path and return their manifests."""
        root = Path(source).expanduser().resolve()
        if not root.exists():
            raise FileNotFoundError(root)
        candidates = (
            [root]
            if root.is_file()
            else (root.rglob("*") if recursive else root.iterdir())
        )
        return [
            self.process_pdf(candidate)
            for candidate in candidates
            if candidate.is_file() and candidate.suffix.lower() == ".pdf"
        ]
