import json
from pathlib import Path

import pytest
from figure_parser.docling_tool import (
    DocumentWorker,
    ExtractionConfig,
    choose_picture,
)
from figure_parser.document_text import (
    context_for,
    figure_label,
    references,
    text_blocks,
)


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


@pytest.mark.parametrize(
    "confidence,destination",
    [(0.79, "below_threshold"), (0.80, "accepted"), (0.95, "accepted")],
)
def test_only_highest_prediction_passes_threshold(confidence, destination):
    predictions = [
        {"class_name": "bar_chart", "confidence": 0.01},
        {
            "class_name": "line_chart",
            "confidence": confidence,
            "created_by": "classifier",
        },
    ]
    selected, route = choose_picture(predictions, ExtractionConfig())
    assert route == destination
    assert selected == (
        {"class_name": "line_chart", "confidence": confidence}
        if confidence >= 0.8
        else None
    )


def test_missing_classification_and_confident_photograph():
    assert choose_picture([], ExtractionConfig()) == (None, "discarded")
    photo = {"class_name": "photograph", "confidence": 0.91}
    assert choose_picture(
        [photo, {"class_name": "line_chart", "confidence": 0.08}], ExtractionConfig()
    ) == (photo, "discarded")
    assert (
        choose_picture(
            [{"class_name": "line_chart", "confidence": 0.6}],
            ExtractionConfig(classification_threshold=0.5),
        )[1]
        == "accepted"
    )


@pytest.mark.parametrize(
    "class_name", ["photograph", "flow_chart", "other", "engineering_drawing"]
)
@pytest.mark.parametrize("confidence", [0.6, 0.95])
def test_non_chart_classes_never_reach_either_image_folder(class_name, confidence):
    assert (
        choose_picture(
            [{"class_name": class_name, "confidence": confidence}], ExtractionConfig()
        )[1]
        == "discarded"
    )


def test_image_routing_and_metadata_export(tmp_path):
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
    from docling_core.types.doc.document import DocItemLabel, ProvenanceItem
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
    doc.add_text(label=DocItemLabel.TITLE, text="A Test Paper", prov=prov)
    caption = doc.add_text(
        label=DocItemLabel.CAPTION, text="Figure 1: A chart", prov=prov
    )
    for score in [0.95, 0.79]:
        picture = doc.add_picture(prov=prov, caption=caption)
        picture.meta = PictureMeta(
            classification=PictureClassificationMetaField(
                predictions=[
                    PictureClassificationPrediction(
                        class_name="bar_chart", confidence=0.01, created_by="test"
                    ),
                    PictureClassificationPrediction(
                        class_name="line_chart", confidence=score, created_by="test"
                    ),
                ]
            )
        )
    doc.add_table(data=TableData(num_rows=0, num_cols=0, table_cells=[]), prov=prov)
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"mock conversion input")
    worker = DocumentWorker(tmp_path / "images")
    worker._converter = SimpleNamespace(
        convert=lambda *args, **kwargs: SimpleNamespace(
            status="success", errors=[], document=doc
        )
    )
    event = worker.process_pdf(source)
    manifest = json.loads(Path(event["manifest_path"]).read_text())
    assert event["asset_count"] == 2 and event["below_threshold_count"] == 1
    assert manifest["assets"][0]["classification"] == {
        "class_name": "line_chart",
        "confidence": 0.95,
    }
    assert manifest["assets"][1]["kind"] == "table"
    below = manifest["below_threshold"][0]
    assert below["classification"] is None
    assert Path(below["images"][0]["path"]).is_relative_to(tmp_path / "below_treshold")
    for asset in [*manifest["assets"], below]:
        assert Path(asset["images"][0]["path"]).is_file()
        assert asset["provenance"] == [{"page_number": 1}]

    def check_no_coordinates(record):
        if isinstance(record, dict):
            assert not {"bbox", "coord_origin", "bounding_box"}.intersection(record)
            for v in record.values():
                check_no_coordinates(v)
        elif isinstance(record, list):
            for v in record:
                check_no_coordinates(v)

    check_no_coordinates(manifest)


class FakeWorker(DocumentWorker):
    calls = 0

    def _extract(self, path, folder, manifest):
        self.calls += 1
        if path.read_bytes() == b"bad":
            raise ValueError("Corrupt test PDF")
        image = folder / "test.png"
        image.write_bytes(b"image content")
        manifest["assets"] = [
            {
                "asset_id": manifest["document_id"] + "-1",
                "status": "completed",
                "image_path": str(image),
            }
        ]
        manifest["status"] = "completed"


def test_each_pdf_notifies_and_failure_does_not_stop_folder(tmp_path):
    source = tmp_path / "input"
    source.mkdir()
    for name, data in [("a.pdf", b"good"), ("b.PDF", b"bad"), ("c.pdf", b"another")]:
        (source / name).write_bytes(data)
    events = []

    def callback(event):
        assert Path(event["manifest_path"]).is_file()
        events.append(event)

    worker = FakeWorker(tmp_path / "images", on_document=callback)
    results = list(worker.iter_folder(source))
    assert len(events) == len(results) == 3
    assert sorted(e["status"] for e in results) == [
        "completed",
        "completed",
        "failed_processing",
    ]
    assert sum(e["ready"] for e in results) == 2


def test_retry_keeps_document_id_and_both_attempts(tmp_path):
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"paper")
    worker = FakeWorker(tmp_path / "images")
    first, second = worker.process_pdf(source), worker.process_pdf(source)
    assert first["document_id"] == second["document_id"]
    assert first["attempt_id"] != second["attempt_id"]
    assert Path(first["manifest_path"]).is_file()
    assert Path(second["manifest_path"]).is_file()


def test_notification_failure_preserves_extraction(tmp_path):
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"paper")

    def callback(event):
        raise ConnectionError("Redis unavailable")

    event = FakeWorker(tmp_path / "images", on_document=callback).process_pdf(source)
    saved = json.loads(Path(event["manifest_path"]).read_text())
    assert event["ready"]
    assert "Redis unavailable" in saved["notification_error"]


def test_missing_source_has_failure_manifest(tmp_path):
    event = FakeWorker(tmp_path / "images").process_pdf(tmp_path / "missing.pdf")
    assert event["status"] == "failed_processing"
    assert Path(event["manifest_path"]).is_file()


def test_no_assets_is_success(tmp_path):
    class EmptyWorker(FakeWorker):
        def _extract(self, path, folder, manifest):
            manifest["status"] = "no_assets"

    source = tmp_path / "paper.pdf"
    source.write_bytes(b"paper")
    result = EmptyWorker(tmp_path / "images").process_pdf(source)
    assert result["ready"] and result["asset_count"] == 0


def test_langgraph_streams_a_failure_before_returning_summary(tmp_path, monkeypatch):
    import figure_parser.docling_graph
    from figure_parser.docling_graph import graph

    monkeypatch.setattr(figure_parser.docling_graph, "DocumentWorker", FakeWorker)

    source = tmp_path / "pdfs"
    source.mkdir()
    (source / "broken.pdf").write_bytes(b"bad")
    events = list(
        graph.stream(
            {"input_path": str(source), "output_dir": str(tmp_path / "images")},
            stream_mode="custom",
        )
    )
    assert len(events) == 1
    assert events[0]["status"] == "failed_processing"


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
