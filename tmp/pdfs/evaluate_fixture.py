"""Run real cached Docling models against the synthetic chart fixture."""

import json
import time
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import fakeredis
from parser.src.docling.docling_tool import DocumentWorker, ExtractionConfig

ROOT = Path(__file__).resolve().parents[2]
PDF = ROOT / "output/pdf/chart_extraction_test_fixture.pdf"
OUT = ROOT / "output/pdf/fixture_evaluation"
EXPECTED = {
    2: ["bar_chart"],
    3: ["line_chart"],
    4: ["pie_chart"],
    5: ["scatter_plot"],
    6: ["box_plot"],
    7: ["heatmap"],
    8: ["bar_chart", "line_chart"],
}


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    client = fakeredis.FakeRedis()
    worker = DocumentWorker(
        config=ExtractionConfig(device="cpu"),
        run_id="synthetic-fixture-evaluation",
        redis_client=client,
    )
    started = time.monotonic()
    recorded = []
    converter = worker._get_converter()

    def record_conversion(*args, **kwargs):
        result = converter.convert(*args, **kwargs)
        document = result.document
        (OUT / "docling_document.json").write_text(
            json.dumps(document.export_to_dict(), indent=2, default=str)
        )
        for item, _ in document.iterate_items():
            label = getattr(item, "label", None)
            label = getattr(label, "value", str(label))
            if label not in {"picture", "table"}:
                continue
            classification = getattr(getattr(item, "meta", None), "classification", None)
            predictions = (
                [prediction.model_dump(mode="json") for prediction in classification.predictions]
                if classification else []
            )
            recorded.append({
                "ref": item.self_ref,
                "label": label,
                "pages": sorted({prov.page_no for prov in item.prov}),
                "predictions": predictions,
            })
        return result

    worker._converter = SimpleNamespace(convert=record_conversion)
    manifest = worker.process_pdf(PDF, publish=False)
    (OUT / "manifest.json").write_text(json.dumps(asdict(manifest), indent=2))
    accepted = []
    for index, asset in enumerate(manifest.assets, 1):
        for ordinal, image in enumerate(asset.images_meta, 1):
            png = client.hget(image.redis_key, "png")
            filename = f"page-{image.page_number}-asset-{index}-{ordinal}.png"
            if png:
                (OUT / filename).write_bytes(png)
            accepted.append({
                "page": image.page_number,
                "class_name": image.class_name,
                "confidence": image.confidence,
                "predictions": image.classifications,
                "width": image.width,
                "height": image.height,
                "crop": filename,
                "context": asdict(asset.context) if asset.context else None,
            })
    rows = []
    for page, expected in EXPECTED.items():
        found = [record for record in recorded if page in record["pages"]]
        kept = [record for record in accepted if record["page"] == page]
        rows.append({"page": page, "expected": expected, "detected": found, "accepted": kept})
    report = {
        "input_pdf": str(PDF.relative_to(ROOT)),
        "scope": "Real CPU Docling conversion/classification; in-memory Redis storage; no VLM, formatter or SQL run",
        "elapsed_seconds": round(time.monotonic() - started, 2),
        "status": manifest.status,
        "errors": manifest.errors,
        "pages": manifest.page_count,
        "expected_chart_panels": sum(map(len, EXPECTED.values())),
        "detected_figures_and_tables": recorded,
        "accepted_crops": len(accepted),
        "by_page": rows,
        "numerical_accuracy": "Not tested: no VLM or formatter is configured",
    }
    (OUT / "results.json").write_text(json.dumps(report, indent=2))
    lines = [
        "# Synthetic fixture evaluation", "", report["scope"], "",
        f"Status: {manifest.status}. Elapsed: {report['elapsed_seconds']} seconds.", "",
        "| PDF page | Expected panels | Detected labels | Accepted labels |",
        "| --- | --- | --- | --- |",
    ]
    for row in rows:
        labels = [
            record["predictions"][0]["class_name"] if record["predictions"] else record["label"]
            for record in row["detected"]
        ]
        lines.append(f"| {row['page']} | {', '.join(row['expected'])} | {', '.join(labels) or 'none'} | "
                     f"{', '.join(record['class_name'] or 'unknown' for record in row['accepted']) or 'none'} |")
    lines += ["", "Numerical accuracy is not tested: no VLM or formatter is configured.",
              "The answer page lists raw box-plot samples, which cannot be recovered individually from the rendered box plot.",
              "Chart-type labels and the answer page make this a diagnostic fixture, not a blind accuracy benchmark."]
    if manifest.errors:
        lines += ["", "Errors:", *[f"- {error}" for error in manifest.errors]]
    (OUT / "report.md").write_text("\n".join(lines) + "\n")
    print(json.dumps({key: report[key] for key in (
        "status", "elapsed_seconds", "pages", "accepted_crops", "errors", "numerical_accuracy"
    )}, indent=2), flush=True)


if __name__ == "__main__":
    main()
