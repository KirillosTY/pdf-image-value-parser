"""Extract a bounded PDF batch in a process that releases Docling's memory on exit."""

import hashlib
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path

from parser.src.docling.docling_tool import DocumentWorker, ExtractionConfig
from parser.src.redis.connection import redis_client
from parser.src.redis.state import manifest_images
from sqlalchemy import create_engine


def extract_batch(payload):
    """Journal each document before proceeding so interrupted batches can resume."""
    client = redis_client()
    engine = create_engine(os.environ["DATABASE_URL"])
    prefix = payload["prefix"]
    try:
        worker = DocumentWorker(
            config=ExtractionConfig(**payload["extraction"]),
            run_id=payload["run_id"], redis_client=client,
        )
        for source in payload["paths"]:
            path = Path(source)
            journal = prefix + ":source:" + hashlib.sha256(str(path).encode()).hexdigest()
            if client.exists(journal):
                continue
            manifest = asdict(worker.process_pdf(path, database_engine=engine, publish=False))
            key = manifest["manifest_key"]
            with client.pipeline(transaction=True) as pipe:
                pipe.set(key, json.dumps(manifest, allow_nan=False))
                pipe.set(journal, key)
                pipe.rpush(prefix + ":manifests", key)
                if manifest["status"] == "completed" and manifest_images(manifest):
                    pipe.sadd(prefix + ":active", key)
                pipe.execute()
    finally:
        client.close()
        engine.dispose()


def main():
    """Accept configuration through stdin without exposing it in process arguments."""
    extract_batch(json.load(sys.stdin))


if __name__ == "__main__":
    main()
