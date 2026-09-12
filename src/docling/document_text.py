"""Collect document-only citation metadata and verbatim figure context."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

LABEL = re.compile(
    r"\b(?P<kind>fig(?:ure)?s?\.?|tables?\.?|tabs?\.?)\s*(?P<number>S?\d+(?:\.\d+)*|[IVX]+)(?!\w)",
    re.I,
)
DOI = re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.I)
ARXIV = re.compile(r"arXiv\s*:\s*((?:\d{4}\.\d{4,5}|[a-z.-]+/\d{7})(?:v\d+)?)", re.I)


def value(obj: Any) -> str:
    """Return an enum's serialized value or its string representation."""
    return str(getattr(obj, "value", obj))


def provenance(item: Any) -> list[dict[str, Any]]:
    """Export only one-based page numbers; keep crop coordinates internal."""
    return [{"page_number": page} for page in sorted({p.page_no for p in item.prov})]


def text_blocks(doc: Any) -> list[dict[str, Any]]:
    """Collect text items in reading order without rewriting their text."""
    result = []
    for order, (item, _) in enumerate(doc.iterate_items()):
        label = value(item.label)
        text = getattr(item, "text", "")
        if label == "formula" and not text.strip():
            text = getattr(item, "orig", "")
        if text.strip() or label == "formula":
            result.append(
                {
                    "item_ref": item.self_ref,
                    "order": order,
                    "label": label,
                    "text": text,
                    "provenance": provenance(item),
                }
            )
    return result


def figure_label(caption: str) -> tuple[str, str] | None:
    """Read a printed figure/table label, without inventing one from item order."""
    match = LABEL.match(caption.strip())
    if not match:
        return None
    kind = "figure" if match["kind"].lower().startswith("fig") else "table"
    return kind, match["number"].upper()


def find_captions(doc: Any, item: Any, kind: str, used: set[str]) -> list[Any]:
    """Recover an unlinked caption only when its label and position agree.

    Some Docling paragraphs contain a caption in a separate provenance span.
    Keep that span verbatim; do not mistake a body reference for a caption.
    """
    if item.captions:
        return [ref.resolve(doc) for ref in item.captions]
    candidates = []
    for text in doc.texts:
        if text.self_ref in used:
            continue
        for prov in text.prov:
            span = text.text[prov.charspan[0] : prov.charspan[1]]
            label = figure_label(span)
            match = LABEL.match(span.strip())
            if not label or label[0] != kind or not match:
                continue
            if value(text.label) != "caption" and not span.strip()[
                match.end() :
            ].lstrip().startswith((":", ".")):
                continue
            cap = prov.bbox.to_top_left_origin(
                page_height=doc.pages[prov.page_no].size.height
            )
            for target in item.prov:
                if target.page_no != prov.page_no:
                    continue
                box = target.bbox.to_top_left_origin(
                    page_height=doc.pages[prov.page_no].size.height
                )
                overlap = min(cap.r, box.r) - max(cap.l, box.l)
                gap = min(abs(cap.t - box.b), abs(box.t - cap.b))
                if overlap >= 0.5 * min(cap.r - cap.l, box.r - box.l) and gap <= 36:
                    candidates.append(
                        (gap, text.model_copy(update={"text": span, "prov": [prov]}))
                    )
    candidates.sort(key=lambda pair: pair[0])
    if not candidates or (
        len(candidates) > 1 and candidates[1][0] - candidates[0][0] < 4
    ):
        return []
    used.add(candidates[0][1].self_ref)
    return [candidates[0][1]]


def references(text: str) -> set[tuple[str, str]]:
    """Match explicit labels, lists, numeric ranges, and panel references."""
    result: set[tuple[str, str]] = set()
    for match in LABEL.finditer(text):
        kind = "figure" if match["kind"].lower().startswith("fig") else "table"
        first = match["number"].upper()
        result.add((kind, first))
        tail = text[match.end() :]
        # A panel suffix belongs to the parent figure (3a / 3(a)).
        tail = re.sub(r"^\s*\([a-z]\)", "", tail, count=1, flags=re.I)
        while True:
            part = re.match(
                r"^\s*(,\s*(?:and\s+)?|and\s+|[–—-])\s*(S?\d+(?:\.\d+)*|[IVX]+)(?!\w)",
                tail,
                re.I,
            )
            if not part:
                break
            number = part[2].upper()
            if part[1] in {"-", "–", "—"} and first.isdigit() and number.isdigit():
                if 0 < int(number) - int(first) <= 100:
                    result.update(
                        (kind, str(n)) for n in range(int(first), int(number) + 1)
                    )
            result.add((kind, number))
            first, tail = number, tail[part.end() :]
    # Unparenthesized panel suffixes, e.g. Fig. 3a, should also link to Fig. 3.
    for match in re.finditer(r"\b(fig(?:ure)?s?\.?)\s*(S?\d+)[a-z]\b", text, re.I):
        result.add(("figure", match[2].upper()))
    return result


def mention_paragraphs(
    blocks: list[dict[str, Any]], label: tuple[str, str] | None
) -> list[dict[str, Any]]:
    """Join interrupted mention paragraphs using existing text and formulas.

    Keep whole blocks when sentence boundaries are uncertain. Reading order
    permits continuation across columns/pages; headings and list items remain
    boundaries. Footnotes, captions, and running headers do not interrupt prose.
    """
    if label is None:
        return []
    groups: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for block in blocks:
        kind = block["label"]
        if kind in {"page_header", "page_footer", "footnote", "caption"}:
            continue
        if kind not in {"text", "paragraph", "list_item", "formula"}:
            current = []
            continue
        if current:
            previous = current[-1]
            end = previous["text"].rstrip()
            # A period in an abbreviation is not a sentence boundary.
            closed = bool(re.search(r"[.!?][\"'’”\])}]*$", end)) and not bool(
                re.search(r"\b(?:figs?|tabs?|eqs?|e\.g|i\.e|et al)\.$", end, re.I)
            )
            starts_lower = bool(re.match(r"\s*[a-z]", block["text"]))
            if (
                kind == "list_item"
                or previous["label"] == "list_item"
                or (closed and not starts_lower)
            ):
                current = []
        if not current:
            current = []
            groups.append(current)
        current.append(block)

    mentions = []
    for group in groups:
        text = " ".join(
            b["text"] if b["text"].strip() else "[equation text unavailable]"
            for b in group
        )
        if label not in references(text):
            continue
        anchor = next((b for b in group if label in references(b["text"])), group[0])
        if len(group) == 1:
            mentions.append(anchor)
            continue
        mentions.append(
            {
                **anchor,
                "text": text,
                "item_refs": [b["item_ref"] for b in group],
                "provenance": [
                    {"page_number": page}
                    for page in sorted(
                        {p["page_number"] for b in group for p in b["provenance"]}
                    )
                ],
            }
        )
    return mentions


def context_for(
    blocks: list[dict[str, Any]],
    captions: list[dict[str, Any]],
    label: tuple[str, str] | None,
    pages: set[int],
    order: int,
    nearby_count: int = 2,
) -> dict[str, Any]:
    """Keep captions, explicit mentions across the paper, and nearby body text."""
    body = [b for b in blocks if b["label"] in {"text", "paragraph", "list_item"}]
    mentions = mention_paragraphs(blocks, label)
    same_page = [
        b for b in body if any(p["page_number"] in pages for p in b["provenance"])
    ]
    before = (
        [b for b in same_page if b["order"] < order][-nearby_count:]
        if nearby_count
        else []
    )
    after = [b for b in same_page if b["order"] > order][:nearby_count]
    return {
        "captions": captions,
        "mentions": mentions,
        "nearby": before + after,
        "text_policy": "verbatim_docling_text; mentions join continuation blocks with spaces and mark unavailable equation text; no generated summary",
        "matching_policy": "explicit figure/table labels; nearby does not imply relevance",
    }


def document_metadata(path: Path, blocks: list[dict[str, Any]]) -> dict[str, Any]:
    """Extract supported fields conservatively; leave uncertain fields empty.

    PDF/XMP fields and identified front-matter spans are the only sources.
    An arbitrary year, DOI in the bibliography, or PDF creation timestamp is
    never treated as the paper's publication information.
    """
    from pypdf import PdfReader

    fields: dict[str, Any] = {
        "title": None,
        "authors": [],
        "publication_year": None,
        "journal": None,
        "doi": None,
        "arxiv_id": None,
        "abstract": None,
        "evidence": {},
        "warnings": [],
        "organization": None,
        "publication_date": None,
    }

    def set_field(name: str, content: Any, source: Any) -> None:
        if content and not fields[name]:
            fields[name] = content
            fields["evidence"][name] = source

    first = [b for b in blocks if any(p["page_number"] == 1 for p in b["provenance"])]
    for block in first:
        if block["label"] == "title":
            set_field("title", block["text"], block)
            break

    try:
        with path.open("rb") as stream:
            reader = PdfReader(stream)
            info = reader.metadata
            xmp = reader.xmp_metadata
            if xmp:
                titles = xmp.dc_title or {}
                set_field(
                    "title",
                    titles.get("x-default") or next(iter(titles.values()), None),
                    "pdf.xmp.dc:title",
                )
                set_field("authors", xmp.dc_creator, "pdf.xmp.dc:creator")
                publisher = getattr(xmp, "dc_publisher", None) or []
                if isinstance(publisher, str):
                    publisher = [publisher]
                set_field("organization", next(iter(publisher), None), "pdf.xmp.dc:publisher")
                # pypdf preserves the XML tree, including publisher-specific fields.
                for node in xmp.rdf_root.getElementsByTagName("*"):
                    ns = node.namespaceURI or ""
                    local = node.localName
                    content = "".join(
                        c.data for c in node.childNodes if c.nodeType == c.TEXT_NODE
                    ).strip()
                    if "prismstandard.org/namespaces" in ns:
                        if local == "publicationName":
                            set_field(
                                "journal", content, "pdf.xmp.prism:publicationName"
                            )
                        elif local == "doi" and DOI.fullmatch(content):
                            set_field("doi", content, "pdf.xmp.prism:doi")
                        elif local == "publicationDate" and re.match(r"\d{4}", content):
                            set_field("publication_date", content, "pdf.xmp.prism:publicationDate")
                            set_field(
                                "publication_year",
                                int(content[:4]),
                                "pdf.xmp.prism:publicationDate",
                            )
            if info:
                set_field("title", info.title, "pdf.info.Title")
                # Preserve unsplit author strings when the PDF does not specify a list.
                if info.author:
                    set_field(
                        "authors",
                        [info.author],
                        "pdf.info.Author (may be an unsplit list)",
                    )
    except Exception as exc:
        fields["warnings"].append(
            f"PDF metadata read failed: {type(exc).__name__}: {exc}"
        )

    for block in first:
        text = block["text"]
        if re.match(r"^(references|bibliography)\b", text.strip(), re.I):
            break
        match = ARXIV.search(text)
        if match:
            set_field("arxiv_id", match[1], block)
        # Only an explicit DOI label/link in front matter or a page header/footer.
        is_front = block["label"] in {"page_header", "page_footer"} or not re.search(
            r"\b(?:abstract|introduction)\b",
            " ".join(b["text"] for b in first if b["order"] < block["order"]),
            re.I,
        )
        if is_front:
            ids = (
                DOI.findall(text)
                if re.search(r"(?:doi\s*:|doi\.org/)", text, re.I)
                else []
            )
            if len(set(ids)) == 1:
                set_field("doi", ids[0].rstrip(".,;"), block)
            date = re.search(
                r"\b(?:published(?:\s+online)?|publication\s+date)\s*:?[^\n]{0,35}?\b((?:19|20)\d{2})\b",
                text,
                re.I,
            )
            if date:
                set_field("publication_year", int(date[1]), block)
            venue = re.match(r"\s*(?:journal|published in)\s*:\s*(.+)", text, re.I)
            if venue:
                set_field("journal", venue[1], block)
            authors = re.match(r"\s*authors?\s*:\s*(.+)", text, re.I)
            if authors:
                set_field("authors", [authors[1]], block)
            organization = re.match(
                r"\s*(?:organization|institution|company|publisher)\s*:\s*(.+)",
                text,
                re.I,
            )
            if organization:
                set_field("organization", organization[1], block)

    abstract: list[dict[str, Any]] = []
    collecting = False
    for block in blocks:
        text = block["text"].strip()
        if re.match(r"^abstract\b", text, re.I):
            collecting = True
            if not re.fullmatch(r"abstract\s*[:.—–-]?", text, re.I):
                abstract.append(block)
            continue
        if collecting:
            if block["label"] in {"section_header", "title"} or re.match(
                r"^(?:keywords|index terms)\b", text, re.I
            ):
                break
            if block["label"] in {"text", "paragraph"}:
                abstract.append(block)
    if abstract:
        set_field("abstract", "\n\n".join(b["text"] for b in abstract), abstract)
    fields["front_matter"] = first
    return fields
