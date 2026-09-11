"""Shape a slim asset into the text context sent to the VLM."""

from typing import TypedDict

from parser.src.docling.type_format import Asset, DocumentMetadata

DEFAULT_PROMT = (
    "Extract all visible data and observations from this image. "
    "Use free-form text; do not fit the findings into a fixed JSON or database schema.\n"
    "Rules:\n"
    "- Describe the chart or table type in your own words, including mixed types "
    "and separate panels. State uncertainty when the type is unclear.\n"
    "- Record axis labels, scales (including logarithmic scales), units, legends, "
    "categories, series, values, annotations, error bars, and footnotes.\n"
    "- Use the x-axis category labels EXACTLY as printed. Do not invent "
    "dates, years, or names.\n"
    "- Series naming: if there is a legend, use the legend labels. "
    "If no series name is visible, say it is unnamed; do not invent a name.\n"
    "- Read each y-value from the y-axis scale and the bar / marker height. "
    "If numeric labels are printed on the bars, prefer those.\n"
    "- Watch y-axis units: '200K' = 200000, '1.5M' = 1500000, "
    "'2.3B' = 2300000000. Preserve the printed value and unit alongside any "
    "unambiguous numeric conversion. Distinguish estimates from printed values.\n"
    "- Do not output series, categories, or values that do not appear on "
    "the image. Describe non-chart content rather than discarding it.\n"
    "- Keep observations even when their role or association is unclear. Mark "
    "unreadable or ambiguous values explicitly; do not guess.\n"
    "- Keep each observation associated with its panel, series, and category "
    "where visible. Context can help interpretation but is not evidence for "
    "values absent from the image.\n"
)



class VlmContext(TypedDict):
    """Carry the text sent alongside a chart crop to the VLM."""

    id: str
    paper_title: str | None
    paper_abstract: str | None
    caption: str | None
    content: str | None


def asset_to_vlm(asset: Asset, metadata: DocumentMetadata | None = None) -> VlmContext:
    """Assemble the prompt and reference text for one asset."""
    references = " ".join(mention.text for mention in asset.context.mentions)
    nearby = " ".join(block.text for block in asset.context.nearby)
    caption = asset.context.caption or None
    title = metadata.title if metadata else None
    abstract = metadata.abstract if metadata else None
    content = "\n".join(
        part
        for part in (
            DEFAULT_PROMT,
            f"Paper title: {title}" if title else None,
            f"Caption: {caption}" if caption else None,
            f"Direct references close by: {references}" if references else None,
            f"Nearby mentions: {nearby}" if nearby else None,
        )
        if part
    )
    return VlmContext(
        id=asset.asset_id,
        paper_title=title,
        paper_abstract=abstract,
        caption=caption,
        content=content,
    )


