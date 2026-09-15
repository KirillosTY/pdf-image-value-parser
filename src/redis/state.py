"""Store PDF manifests and PNG images, then queue PDFs for the VLM."""

import json
from dataclasses import asdict
from io import BytesIO
from uuid import uuid4

from parser.src.docling.type_format import Manifest
from parser.src.redis.connection import redis_client

red = redis_client()

STREAM = "pdf:ready"
FORMAT_READY = "images:format_ready"


def create_manifest_key(document_id: str) -> str:
    """Create one key per PDF attempt, before storing its images."""
    return f"manifest:{document_id}:{uuid4().hex}"


def store_image(image, manifest_key: str, asset_id: str, ordinal: int, *, client=None) -> str:
    """Store a PNG and its parent manifest key together in a Redis hash."""
    key = f"{manifest_key}:image:{asset_id}:{ordinal}"
    with BytesIO() as buffer:
        image.save(buffer, format="PNG")
        (client if client is not None else red).hset(
            key,
            mapping={
                "manifest_key": manifest_key,
                "png": buffer.getvalue(),
            },
        )
    return key


def queue_manifest(manifest: Manifest, manifest_key: str) -> str:
    """Store the finished manifest before publishing its Stream entry."""
    payload = asdict(manifest)
    payload["manifest_key"] = manifest_key
    red.set(manifest_key, json.dumps(payload, ensure_ascii=False))
    red.xadd(STREAM, {"manifest_key": manifest_key})
    return manifest_key


# A document appears here only when every accepted image has been formatted.
DB_READY = "documents:db_ready"


def manifest_images(manifest: dict) -> list[dict]:
    """Return accepted images, excluding below-threshold assets."""
    return [
        image
        for asset in manifest.get("assets", [])
        for image in asset.get("images_meta", [])
    ]


def find_manifest_image(manifest: dict, image_key: str) -> dict:
    """Find an image belonging to this PDF attempt."""
    for image in manifest_images(manifest):
        if image["redis_key"] == image_key:
            return image
    raise KeyError(f"Image is not in this manifest: {image_key}")


def image_failed(image: dict) -> bool:
    """Identify a terminal failure, distinct from an attempt awaiting retry."""
    return (
        image.get("failed") is True
        and "failed" in {image.get("vlm_status"), image.get("format_status")}
        and "processing" not in {image.get("vlm_status"), image.get("format_status")}
    )


def document_ready(manifest: dict) -> bool:
    """Require all images to succeed or be explicitly omitted after terminal failure."""
    images = manifest_images(manifest)
    return (
        manifest.get("status") == "completed"
        and bool(images)
        and all(
            (
                not image.get("failed")
                and image.get("vlm_status") == "complete"
                and image.get("format_status") == "complete"
            )
            or (manifest.get("approve_with_fails") == "omit" and image_failed(image))
            for image in images
        )
    )


def refresh_format_status(manifest: dict) -> None:
    """Derive document formatting progress from its accepted images."""
    statuses = [
        image.get("format_status", "not_started") for image in manifest_images(manifest)
    ]
    if statuses and all(status == "complete" for status in statuses):
        status = "complete"
    elif "processing" in statuses:
        status = "processing"
    elif "failed" in statuses:
        status = "failed"
    elif "complete" in statuses:
        status = "processing"
    else:
        status = "not_started"
    manifest["format_status"] = status


def load_manifest(client, manifest_key: str) -> dict:
    """Load a saved PDF manifest, failing explicitly if it has disappeared."""
    raw = client.get(manifest_key)
    if raw is None:
        raise KeyError(f"Manifest not found: {manifest_key}")
    return json.loads(raw)


def update_manifest(client, manifest_key: str, update) -> dict:
    """Save a mutation and database eligibility atomically under WATCH."""
    from time import time

    from redis.exceptions import WatchError

    for _ in range(20):
        with client.pipeline() as pipe:
            try:
                pipe.watch(manifest_key)
                manifest = load_manifest(pipe, manifest_key)
                update(manifest)
                refresh_format_status(manifest)
                pipe.multi()
                pipe.set(
                    manifest_key,
                    json.dumps(manifest, ensure_ascii=False, allow_nan=False),
                )
                if document_ready(manifest) and manifest.get("db_status") != "complete":
                    pipe.zadd(DB_READY, {manifest_key: time()}, nx=True)
                else:
                    pipe.zrem(DB_READY, manifest_key)
                pipe.execute()
                return manifest
            except WatchError:
                continue
    raise RuntimeError("Manifest remained busy; retry this operation")


def update_manifest_image(client, manifest_key: str, image_key: str, update) -> dict:
    """Update one image without losing concurrent changes to another image."""

    def mutate(manifest):
        if manifest.get("db_status") == "complete":
            raise ValueError(
                "Stored document is immutable; create a new extraction attempt"
            )
        update(find_manifest_image(manifest, image_key))

    manifest = update_manifest(client, manifest_key, mutate)
    return find_manifest_image(manifest, image_key)


def mark_image_failed(client, manifest_key: str, image_key: str) -> dict:
    """Finalize a failed image after the caller has exhausted its retry policy."""

    def finish(image):
        if "failed" not in {image.get("vlm_status"), image.get("format_status")}:
            raise ValueError("Only a failed processing attempt can be finalized")
        if "processing" in {image.get("vlm_status"), image.get("format_status")}:
            raise ValueError("An image is still processing")
        image["failed"] = True

    return update_manifest_image(client, manifest_key, image_key, finish)


def save_vlm_result(
    client,
    manifest_key: str,
    image_key: str,
    raw_output: str,
    *,
    model: str | None = None,
) -> dict:
    """Commit raw output, VLM completion, and its formatting job together.

    Repeating a successful call is harmless, including after a lost EXEC reply.
    Existing completed results without a job marker can be handed off once.
    """
    from redis.exceptions import WatchError

    if not isinstance(raw_output, str) or not raw_output.strip():
        raise ValueError("VLM output is empty")

    for _ in range(20):
        with client.pipeline() as pipe:
            try:
                pipe.watch(manifest_key, FORMAT_READY)
                manifest = load_manifest(pipe, manifest_key)
                image = find_manifest_image(manifest, image_key)
                if image.get("failed"):
                    raise ValueError(
                        "Image has a terminal failure; create a new attempt"
                    )
                saved = image.get("vlm_result") or {}
                if image.get("vlm_status") == "complete":
                    if saved.get("raw_output") != raw_output:
                        raise ValueError("Cannot replace completed VLM output")
                    if (
                        saved.get("format_job_enqueued")
                        or image.get("format_status") == "complete"
                    ):
                        return image
                elif image.get("format_status") in {"processing", "complete"}:
                    raise ValueError(
                        "Cannot replace VLM output during or after formatting"
                    )
                if manifest.get("db_status") == "complete":
                    raise ValueError("Stored document is immutable")

                # Redis transactions do not roll back command runtime errors.
                # Check the stream type under WATCH before queuing either write.
                if pipe.type(FORMAT_READY) not in {
                    b"none",
                    b"stream",
                    "none",
                    "stream",
                }:
                    raise ValueError("Formatting queue key must be a Redis stream")
                image["vlm_result"] = {
                    **saved,
                    "raw_output": raw_output,
                    "model": saved.get("model", model),
                    "format_job_enqueued": True,
                }
                image["vlm_status"] = "complete"
                payload = json.dumps(manifest, ensure_ascii=False, allow_nan=False)
                pipe.multi()
                pipe.set(manifest_key, payload)
                pipe.xadd(
                    FORMAT_READY,
                    {
                        "manifest_key": manifest_key,
                        "image_key": image_key,
                    },
                )
                pipe.execute()
                return image
            except WatchError:
                continue
    raise RuntimeError(
        "Manifest or formatting queue remained busy; retry this operation"
    )


def manifest_fingerprint(manifest: dict) -> str:
    """Identify a database snapshot, excluding only its acknowledgment flag."""
    from hashlib import sha256

    payload = {key: value for key, value in manifest.items() if key != "db_status"}
    return sha256(
        json.dumps(payload, sort_keys=True, allow_nan=False).encode()
    ).hexdigest()


def mark_document_stored(client, snapshot: dict) -> None:
    """Acknowledge a whole document after its Postgres transaction commits."""
    expected = manifest_fingerprint(snapshot)

    def mark(current):
        if not document_ready(current) or manifest_fingerprint(current) != expected:
            raise ValueError("Document changed after the database batch was built")
        current["db_status"] = "complete"

    update_manifest(client, snapshot["manifest_key"], mark)
