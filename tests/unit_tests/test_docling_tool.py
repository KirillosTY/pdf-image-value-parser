import json

import pytest
from figure_parser.docling_tool import (
    DocumentWorker,
    ExtractionConfig,
)
from figure_parser.document_text import (
    context_for,
    figure_label,
    references,
    text_blocks,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles


@compiles(JSONB, "sqlite")
def compile_jsonb_for_sqlite(type_, compiler, **kwargs):
    """Use SQLite JSON when exercising run recovery locally."""
    return "JSON"


@pytest.mark.parametrize(
    "text,expected",
    [
        ("See Fig. 3 on the next page.", {("figure", "3")}),
        (
            "Figures 2–4 and 7 show the results.",
            {("figure", str(i)) for i in (2, 3, 4, 7)},
        ),
        ("Tables I and IV", {("table", "I"), ("table", "IV")}),
        ("Fig. S2 and Table 10", {("figure", "S2"), ("table", "10")}),
        ("Fig. 3(a) and Fig. 3b", {("figure", "3")}),
        ("We used 3 trials and 10 samples.", set()),
    ],
)
def test_reference_matching(text, expected):
    assert references(text) == expected


def test_label_is_not_an_invented_ordinal():
    assert figure_label("Figure S3: Spectrum.") == ("figure", "S3")
    assert figure_label("The spectrum is shown here.") is None


def test_context_cross_page_and_verbatim():
    def block(text, order, page):
        return {
            "text": text,
            "order": order,
            "label": "text",
            "item_ref": str(order),
            "provenance": [{"page_number": page}],
        }

    distant = block("The values in Fig. 3 are 1.50 ± 0.03.\nSecond line.", 2, 1)
    unrelated = block("Fig. 30 is different.", 4, 1)
    nearby = block("Nearby material.", 9, 5)
    context = context_for([distant, unrelated, nearby], [], ("figure", "3"), {5}, 10)
    assert context["mentions"] == [distant]
    assert context["nearby"] == [nearby]
    assert context["mentions"][0]["text"] == distant["text"]


def mention_block(text, order, label="text", page=1):
    return {
        "text": text,
        "order": order,
        "label": label,
        "item_ref": f"#/texts/{order}",
        "provenance": [{"page_number": page}],
    }


@pytest.mark.parametrize("equation", [r"r_t = \\log(p_t/p_{t-1})", ""])
def test_mentions_join_equation_and_paragraph_across_pages(equation):
    prefix = mention_block("The experiment uses stock prices. Returns defined by", 1)
    footnote = mention_block("1 An unrelated footnote.", 2, "footnote")
    formula = mention_block(equation, 3, "formula")
    header = mention_block("Paper title", 4, "page_header", 2)
    suffix = mention_block("are plotted in Figure 3. Ten stocks were used.", 5, page=2)
    unrelated = mention_block("A separate paragraph.", 6, page=2)
    caption = {"text": "Figure 3: Returns", "item_ref": "#/texts/9", "provenance": []}
    blocks = [prefix, footnote, formula, header, suffix, unrelated]
    context = context_for(blocks, [caption], ("figure", "3"), {2}, 7)
    mention = context["mentions"][0]
    assert len(context["mentions"]) == 1
    assert mention["text"] == " ".join(
        [prefix["text"], equation or "[equation text unavailable]", suffix["text"]]
    )
    assert mention["item_refs"] == ["#/texts/1", "#/texts/3", "#/texts/5"]
    assert mention["provenance"] == [{"page_number": 1}, {"page_number": 2}]
    assert mention["item_ref"] == suffix["item_ref"]
    assert context["captions"] == [caption]
    assert context["nearby"] == [suffix, unrelated]
    assert suffix["text"].startswith("are plotted")  # Source blocks are unchanged.


def test_mention_before_equation_includes_following_continuation_once():
    blocks = [
        mention_block("Table 2 lists estimates of", 1),
        mention_block("x + y", 2, "formula"),
        mention_block("for each trial in Table 2.", 3),
        mention_block("This is a separate paragraph.", 4),
    ]
    mentions = context_for(blocks, [], ("table", "2"), {1}, 5)["mentions"]
    assert len(mentions) == 1
    assert (
        mentions[0]["text"]
        == "Table 2 lists estimates of x + y for each trial in Table 2."
    )


@pytest.mark.parametrize("boundary", ["section_header", "title", "list_item"])
def test_mentions_do_not_join_across_structural_boundaries(boundary):
    fragment = mention_block("are shown in Figure 3.", 3)
    blocks = [
        mention_block("Unfinished material", 1),
        mention_block("A new section or list entry", 2, boundary),
        fragment,
    ]
    assert context_for(blocks, [], ("figure", "3"), {1}, 4)["mentions"] == [fragment]


def test_reference_label_split_between_blocks_is_found():
    blocks = [mention_block("See Fig.", 1), mention_block("3 for results.", 2, page=2)]
    mention = context_for(blocks, [], ("figure", "3"), {2}, 3)["mentions"][0]
    assert mention["text"] == "See Fig. 3 for results."


def test_empty_formula_is_kept_and_existing_original_text_is_used():
    from types import SimpleNamespace

    items = [
        SimpleNamespace(
            label="formula", text="", orig=original, self_ref=f"#/texts/{i}", prov=[]
        )
        for i, original in enumerate(["", r"\alpha + \beta"])
    ]
    doc = SimpleNamespace(iterate_items=lambda: [(item, 0) for item in items])
    blocks = text_blocks(doc)
    assert [b["text"] for b in blocks] == ["", r"\alpha + \beta"]
    assert all(b["label"] == "formula" for b in blocks)


@pytest.fixture
def redis_client(monkeypatch):
    import fakeredis
    from parser.src.redis import state

    client = fakeredis.FakeRedis()
    monkeypatch.setattr(state, "red", client)
    return client


def test_extracted_images_and_manifest_are_saved_in_redis(tmp_path, redis_client):
    from io import BytesIO
    from types import SimpleNamespace

    from docling_core.types.doc import (
        DoclingDocument,
        ImageRef,
        PictureClassificationMetaField,
        PictureClassificationPrediction,
        PictureMeta,
        TableData,
    )
    from docling_core.types.doc.base import BoundingBox, CoordOrigin, Size
    from docling_core.types.doc.document import ProvenanceItem
    from PIL import Image

    doc = DoclingDocument(name="test")
    doc.add_page(
        page_no=1,
        size=Size(width=600, height=800),
        image=ImageRef.from_pil(Image.new("RGB", (600, 800), "white"), dpi=72),
    )
    prov = ProvenanceItem(
        page_no=1,
        charspan=(0, 0),
        bbox=BoundingBox(l=100, t=100, r=400, b=200, coord_origin=CoordOrigin.TOPLEFT),
    )
    picture = doc.add_picture(prov=prov)
    picture.meta = PictureMeta(
        classification=PictureClassificationMetaField(
            predictions=[
                PictureClassificationPrediction(
                    class_name="line_chart", confidence=0.95, created_by="test"
                )
            ]
        )
    )
    doc.add_table(data=TableData(num_rows=0, num_cols=0, table_cells=[]), prov=prov)
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"mock conversion input")
    worker = DocumentWorker(config=ExtractionConfig(device="cpu"))
    worker._converter = SimpleNamespace(
        convert=lambda *args, **kwargs: SimpleNamespace(
            status="success", errors=[], document=doc
        )
    )

    result = worker.process_pdf(source)
    assert result.status == "completed"
    saved = json.loads(redis_client.get(result.manifest_key))
    assert len(saved["assets"]) == 2
    assert saved["db_status"] == "not_started"
    for asset in saved["assets"]:
        image = asset["images_meta"][0]
        assert image["manifest_key"] == result.manifest_key
        assert image["vlm_status"] == "not_started"
        assert "db_status" not in image
        stored = redis_client.hgetall(image["redis_key"])
        assert stored[b"manifest_key"].decode() == result.manifest_key
        with Image.open(BytesIO(stored[b"png"])) as crop:
            assert crop.format == "PNG"
            assert crop.size == (image["width"], image["height"])
    stream = redis_client.xrange("pdf:ready")
    assert len(stream) == 1
    assert stream[0][1][b"manifest_key"].decode() == result.manifest_key
    assert list(tmp_path.iterdir()) == [source]


class FakeWorker(DocumentWorker):
    def _extract(self, path, manifest):
        from figure_parser.type_format import Asset

        if path.read_bytes() == b"bad":
            raise ValueError("Corrupt test PDF")
        if path.read_bytes() == b"empty":
            manifest.status = "no_assets"
            return
        manifest.assets = [Asset(asset_id="test", name="figure", status="completed")]
        manifest.status = "completed"


def test_folder_continues_after_bad_pdf_and_only_queues_successes(
    tmp_path, redis_client
):
    for name, data in [("a.pdf", b"good"), ("b.PDF", b"bad"), ("c.pdf", b"empty")]:
        (tmp_path / name).write_bytes(data)
    (tmp_path / "ignored.txt").write_text("not a PDF")
    results = FakeWorker().process_folder(tmp_path)
    assert sorted(result.status for result in results) == [
        "completed",
        "failed_processing",
        "no_assets",
    ]
    stream = redis_client.xrange("pdf:ready")
    assert len(stream) == 1
    completed = next(result for result in results if result.status == "completed")
    assert stream[0][1][b"manifest_key"].decode() == completed.manifest_key
    for result in results:
        if result.status != "completed":
            assert redis_client.get(result.manifest_key) is None


def test_retry_preserves_document_identity_and_creates_new_manifest(
    tmp_path, redis_client
):
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"paper")
    worker = FakeWorker()
    first, second = worker.process_pdf(source), worker.process_pdf(source)
    assert first.document_id == second.document_id
    assert first.manifest_key != second.manifest_key
    assert redis_client.exists(first.manifest_key, second.manifest_key) == 2


def test_missing_pdf_returns_failure_without_queueing(tmp_path, redis_client):
    result = FakeWorker().process_pdf(tmp_path / "missing.pdf")
    assert result.status == "failed_processing"
    assert result.errors
    assert redis_client.xlen("pdf:ready") == 0


def test_resume_skips_only_a_document_stored_in_the_same_incomplete_run(
    tmp_path, redis_client
):
    from parser.src.db import runs, tables
    from sqlalchemy import create_engine

    engine = create_engine("sqlite://")
    tables.metadata.create_all(engine)
    run_id = runs.start_run(engine, run_id="run-a")
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"paper")
    first = FakeWorker(run_id=run_id).process_pdf(source)
    redis_client.flushall()
    with engine.begin() as connection:
        connection.execute(
            tables.documents.insert().values(
                manifest_key="stored-manifest",
                document_id=first.document_id,
                run_id=run_id,
                pdf_sha256=first.pdf_sha256,
                source_path=str(source),
                metadata={},
                snapshot_hash="stored-snapshot",
            )
        )
    resumed = FakeWorker(run_id=run_id).process_pdf(
        source, database_engine=engine, resume=True
    )
    assert resumed.status == "already_stored"
    assert resumed.db_status == "complete"
    assert redis_client.xlen("pdf:ready") == 0


def test_caption_fallback_uses_the_exact_span_and_rejects_body_mentions():
    from types import SimpleNamespace

    from docling_core.types.doc import DoclingDocument
    from docling_core.types.doc.base import BoundingBox, CoordOrigin, Size
    from docling_core.types.doc.document import DocItemLabel, ProvenanceItem
    from figure_parser.document_text import find_captions

    doc = DoclingDocument(name="test")
    doc.add_page(page_no=1, size=Size(width=600, height=800))
    text = "Some body text. Figure 2: Exact caption."
    start = text.index("Figure")
    prov = ProvenanceItem(
        page_no=1,
        charspan=(start, len(text)),
        bbox=BoundingBox(l=100, t=205, r=400, b=220, coord_origin=CoordOrigin.TOPLEFT),
    )
    doc.add_text(label=DocItemLabel.TEXT, text=text, prov=prov)
    target = SimpleNamespace(
        captions=[],
        prov=[
            ProvenanceItem(
                page_no=1,
                charspan=(0, 0),
                bbox=BoundingBox(
                    l=100, t=100, r=400, b=200, coord_origin=CoordOrigin.TOPLEFT
                ),
            )
        ],
    )
    used = set()
    assert (
        find_captions(doc, target, "figure", used)[0].text == "Figure 2: Exact caption."
    )
    assert not find_captions(doc, target, "figure", used)
    assert not find_captions(doc, target, "table", set())
    doc.texts[0].text = "Figure 2 shows an interesting result."
    doc.texts[0].prov[0].charspan = (0, len(doc.texts[0].text))
    assert not find_captions(doc, target, "figure", set())


def test_metadata_does_not_use_cited_papers_doi_or_year(tmp_path):
    from figure_parser.document_text import document_metadata

    def block(text, label, order):
        return {
            "text": text,
            "label": label,
            "order": order,
            "provenance": [{"page_number": 1}],
        }

    blocks = [
        block("A Scientific Work", "title", 0),
        block("Abstract", "section_header", 1),
        block("We improve the 2020 result (doi:10.1234/example).", "text", 2),
        block("Introduction", "section_header", 3),
    ]
    metadata = document_metadata(tmp_path / "unavailable.pdf", blocks)
    assert metadata["title"] == "A Scientific Work"
    assert metadata["doi"] is None and metadata["publication_year"] is None
    assert metadata["abstract"] == blocks[2]["text"]
