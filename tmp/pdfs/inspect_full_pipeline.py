"""Export real app state from the isolated test services without credentials."""
import json
import os
from pathlib import Path

from dotenv import load_dotenv
from parser.src.db import tables
from parser.src.db.runs import load_run_context
from parser.src.db.schemas import output_tables
from redis import Redis
from sqlalchemy import create_engine, select


load_dotenv()
engine = create_engine(os.environ["DATABASE_URL"])
client = Redis.from_url(os.environ["REDIS_URL"])
with engine.connect() as connection:
    runs = connection.execute(select(tables.run_info).order_by(tables.run_info.c.started_at.desc())).mappings().all()
    if not runs:
        raise SystemExit("No app run found")
    run = dict(runs[0])
    run_id = run["run_id"]
    folder = Path("output/pdf/full_pipeline") / run_id
    folder.mkdir(parents=True, exist_ok=True)
    context = load_run_context(engine, run_id)
    documents = [dict(row) for row in connection.execute(select(tables.documents).where(tables.documents.c.run_id == run_id)).mappings()]
    images = [dict(row) for row in connection.execute(select(tables.images).join(tables.documents).where(tables.documents.c.run_id == run_id)).mappings()]
    details = [dict(row) for row in connection.execute(select(tables.extraction_details).select_from(tables.extraction_details.join(tables.images, tables.extraction_details.c.image_key == tables.images.c.image_key).join(tables.documents, tables.images.c.manifest_key == tables.documents.c.manifest_key)).where(tables.documents.c.run_id == run_id)).mappings()]
    outputs = {}
    for name, table in output_tables(context["schema"]).items():
        outputs[name] = [dict(row) for row in connection.execute(select(table).select_from(table.join(tables.images, table.c.image_key == tables.images.c.image_key).join(tables.documents, tables.images.c.manifest_key == tables.documents.c.manifest_key)).where(tables.documents.c.run_id == run_id)).mappings()]
manifests = [json.loads(client.get(key)) for key in client.lrange(f"run_queue:{run_id}:manifests", 0, -1)]
events = [json.loads(fields[b"data"]) for _, fields in client.xrange(f"run_queue:{run_id}:events")]
state = {"run": run, "documents": documents, "images": images, "details": details, "outputs": outputs, "manifests": manifests, "events": events}
(folder / "state.json").write_text(json.dumps(state, indent=2, default=str) + "\n")
Path("output/pdf/full_pipeline/latest_run.txt").write_text(run_id + "\n")
summary = {"run_id": run_id, "status": run["status"], "outcome": run["outcome"], "errors": run["errors"], "stored_documents": len(documents), "stored_images": len(images), "stored_measurements": sum(map(len, outputs.values())), "images": []}
for manifest in manifests:
    for asset in manifest["assets"]:
        for image in asset.get("images_meta", []):
            page = image["page_number"]
            summary["images"].append({k: image.get(k) for k in ["page_number", "class_name", "vlm_status", "format_status", "vlm_retry_count", "format_retry_count", "failed", "error"]})
            png = client.hget(image["redis_key"], "png")
            if png:
                (folder / f"page-{page}.png").write_bytes(png)
            if image.get("vlm_result"):
                (folder / f"page-{page}-vlm.txt").write_text(image["vlm_result"]["raw_output"])
            if image.get("formatting"):
                (folder / f"page-{page}-formatted.json").write_text(json.dumps(image["formatting"], indent=2) + "\n")
print(json.dumps(summary, indent=2, default=str))
engine.dispose()
client.close()
