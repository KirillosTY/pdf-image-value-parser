"""Store PDF manifests and PNG images, then queue PDFs for the VLM."""

import json
from dataclasses import asdict
from io import BytesIO
from uuid import uuid4

import redis

from parser.src.docling.type_format import Manifest

red = redis.Redis(host="localhost", port=6379, decode_responses=False)

STREAM = "pdf:ready"


def create_manifest_key(document_id: str) -> str:
    """Create one key per PDF attempt, before storing its images."""
    return f"manifest:{document_id}:{uuid4().hex}"


def store_image(image, manifest_key: str, asset_id: str, ordinal: int) -> str:
    """Store a PNG and its parent manifest key together in a Redis hash."""
    key = f"{manifest_key}:image:{asset_id}:{ordinal}"
    with BytesIO() as buffer:
        image.save(buffer, format="PNG")
        red.hset(key, mapping={
            "manifest_key": manifest_key,
            "png": buffer.getvalue(),
        })
    return key


def queue_manifest(manifest: Manifest, manifest_key: str) -> str:
    """Store the finished manifest before publishing its Stream entry."""
    payload = asdict(manifest)
    payload["manifest_key"] = manifest_key
    red.set(manifest_key, json.dumps(payload, ensure_ascii=False))
    red.xadd(STREAM, {"manifest_key": manifest_key})
    return manifest_key
