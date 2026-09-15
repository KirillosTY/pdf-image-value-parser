"""Run recoverable producers, per-model consumers and whole-document writes."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

from parser.src.db.db import document_rows, write_batches
from parser.src.db.runs import finish_run, load_run_context
from parser.src.docling.docling_tool import DocumentWorker, ExtractionConfig
from parser.src.redis.connection import async_redis_client, redis_client
from parser.src.redis.queues import WORKER_GROUP
from parser.src.redis.state import (
    find_manifest_image,
    manifest_images,
    mark_document_stored,
    update_manifest_image,
)
from redis.exceptions import ResponseError
from sqlalchemy import create_engine

from agent import inference
from agent.resources import ModelServers, compatible_groups, endpoint_options, validate_resource_plan
from agent.routing import route_image, route_result
from agent.state import State, WorkerEvent

# One runtime owns a run lease. The lease prevents two graph processes from
# consuming that run simultaneously; durable queues survive runtime replacement.
_RUNTIMES = {}
_ONCE = """
if redis.call('exists', KEYS[1]) == 1 then return 0 end
redis.call('xadd', KEYS[2], '*', 'data', ARGV[1])
redis.call('set', KEYS[1], '1')
return 1
"""
_LEASE = """
if redis.call('get', KEYS[1]) ~= ARGV[1] then return 0 end
return redis.call('expire', KEYS[1], 60)
"""
_RELEASE = """
if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) end
return 0
"""


def decoded(value):
    """Decode a Redis key without changing already-decoded values."""
    return value.decode() if isinstance(value, bytes) else value


async def run_together(coroutines):
    """Cancel sibling requests before releasing shared model resources."""
    tasks = [asyncio.create_task(coroutine) for coroutine in coroutines]
    try:
        return await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


class Runtime:
    """Own live resources outside checkpointable graph state."""

    def __init__(self, state, *, client=None, sync_client=None, engine=None):
        """Create the run's connections without launching workers yet."""
        self.state = State(**asdict(state))
        self.config = state.config
        self.client = client if client is not None else async_redis_client()
        self.sync = sync_client if sync_client is not None else redis_client()
        self.engine = (
            engine if engine is not None else create_engine(os.environ["DATABASE_URL"])
        )
        self.prefix = f"run_queue:{quote(state.run_id, safe='')}"
        self.events = self.prefix + ":events"
        self.token = uuid4().hex
        self.tasks = []
        self.threads = set()
        self.closed = False
        self.wakeup = asyncio.Event()
        self.delivery = None
        self.context = None
        self.failure = None
        self.transport_failure = None
        self.model_limits = {
            key: asyncio.Semaphore(model.get("requests_per_model", 1))
            for key, model in self.config["models"].items()
        }

    def key(self, suffix):
        """Namespace every runtime record by run identity."""
        return f"{self.prefix}:{suffix}"

    async def blocking(self, function, *args, **kwargs):
        """Track synchronous I/O so shutdown never closes its resources early."""
        task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
        self.threads.add(task)
        task.add_done_callback(self.threads.discard)
        return await asyncio.shield(task)

    async def start(self):
        """Acquire ownership before restoring the producer and supervisors."""
        if not await self.client.set(self.key("lease"), self.token, nx=True, ex=60):
            raise RuntimeError("This run already has an active worker supervisor")
        try:
            await self.client.xgroup_create(
                self.events, "coordinator", id="0-0", mkstream=True
            )
        except ResponseError as exc:
            if not str(exc).startswith("BUSYGROUP"):
                raise
        self.context = await self.blocking(
            load_run_context, self.engine, self.state.run_id
        )
        if self.context["status"] == "complete":
            from agent.coordinator import pipeline_is_finished

            if not pipeline_is_finished(self.state):
                raise ValueError("Cannot restart a completed run")
            return
        for key in set(self.state.vlm_queues) | set(self.state.formatter_queues):
            endpoint_options(self.config["models"][key])
        model_keys = set(self.state.vlm_queues) | set(self.state.formatter_queues)
        if self.state.resource_plan:
            validate_resource_plan(
                self.config, self.state.current_hardware, model_keys,
                self.state.resource_plan,
            )
        else:
            self.state.resource_plan = compatible_groups(
                self.config, self.state.current_hardware, model_keys,
            )
        workers = [("lease", self.heartbeat), ("docling", self.produce)]
        if not self.state.graph_managed_stages:
            workers.extend([
                ("router", self.route_manifests),
                ("models", self.consume_models),
                ("writer", self.write_ready),
            ])
        self.tasks = [
            asyncio.create_task(self.guard(name, method), name=name)
            for name, method in workers
        ]

    async def guard(self, source, method):
        """Surface worker death as an explicit fatal event."""
        try:
            await method()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.failure = exc
            for task in self.tasks:
                if task is not asyncio.current_task() and task.get_name() != "lease":
                    task.cancel()
            try:
                await self.emit(
                    "worker.failed", source=source, error=f"{type(exc).__name__}: {exc}"
                )
            except Exception as transport_error:
                self.transport_failure = transport_error

    async def heartbeat(self):
        """Renew the run lease while this supervisor owns its resources."""
        while True:
            await asyncio.sleep(10)
            if not await self.client.eval(_LEASE, 1, self.key("lease"), self.token):
                raise RuntimeError("Worker supervisor lost its run lease")

    async def enqueue_once(self, queue, identity, payload):
        """Publish a job and its deduplication marker in one Redis operation."""
        digest = hashlib.sha256(identity.encode()).hexdigest()
        await self.client.eval(
            _ONCE,
            2,
            self.key("sent:" + digest),
            queue,
            json.dumps(payload, allow_nan=False),
        )
        self.wakeup.set()

    async def emit(self, kind, *, source=None, **payload):
        """Publish a stable event only once, including after a replayed action."""
        identity = ":".join(
            [
                kind,
                payload.get("document_attempt_id", ""),
                payload.get("image_key", ""),
                source or kind.split(".")[0],
            ]
        )
        event = WorkerEvent(
            event_id=identity,
            run_id=self.state.run_id,
            kind=kind,
            source=source or kind.split(".")[0],
            timestamp=datetime.now(timezone.utc).isoformat(),
            **payload,
        )
        await self.enqueue_once(self.events, "event:" + identity, asdict(event))

    async def claim(self, stream, group, consumer, *, block=None):
        """Recover this consumer's oldest pending job before accepting new work."""
        result = await self.client.xreadgroup(group, consumer, {stream: "0"}, count=1)
        if not result or not result[0][1]:
            kwargs = {"block": block} if block else {}
            result = await self.client.xreadgroup(
                group, consumer, {stream: ">"}, count=1, **kwargs
            )
        if not result or not result[0][1]:
            return None
        entry_id, fields = result[0][1][0]
        return entry_id, json.loads(fields.get(b"data", fields.get("data")))

    async def next_event(self):
        """Wait asynchronously and retain delivery until graph actions succeed."""
        while True:
            job = await self.claim(self.events, "coordinator", "main", block=1000)
            if job:
                self.delivery = job[0]
                return WorkerEvent(**job[1])
            if self.transport_failure:
                raise RuntimeError(
                    "Worker event transport failed"
                ) from self.transport_failure
            await self.pause()

    async def acknowledge_event(self, event):
        """Acknowledge the current pending delivery after its checkpointed actions."""
        if await self.client.sismember(self.key("acked_events"), event.event_id):
            self.delivery = None
            return
        if self.delivery is None:
            # Recovery can resume at the acknowledgment action itself.
            job = await self.claim(self.events, "coordinator", "main")
            if job is None:
                return
            if job[1]["event_id"] != event.event_id:
                raise RuntimeError("Pending event does not match the graph checkpoint")
            self.delivery = job[0]
        async with self.client.pipeline(transaction=True) as pipe:
            pipe.xack(self.events, "coordinator", self.delivery)
            pipe.sadd(self.key("acked_events"), event.event_id)
            await pipe.execute()
        self.delivery = None

    async def pause(self):
        """Yield without busy polling; Redis remains the source of truth."""
        try:
            await asyncio.wait_for(self.wakeup.wait(), timeout=0.2)
        except asyncio.TimeoutError:
            pass
        self.wakeup.clear()

    async def manifests(self):
        """Read the run's publication order, independent of historical streams."""
        return [
            decoded(key)
            for key in await self.client.lrange(self.key("manifests"), 0, -1)
        ]

    async def manifest(self, key):
        """Read a durable manifest from Redis."""
        raw = await self.client.get(key)
        if raw is None:
            raise KeyError(f"Manifest disappeared: {key}")
        return json.loads(raw)

    async def produce(self):
        """Extract incrementally, keeping at most 50 unfinished manifests resident."""
        if await self.client.exists(self.key("producer_done")):
            return
        root = Path(self.state.input_path).expanduser().resolve()
        if not root.exists():
            raise FileNotFoundError(root)
        candidates = (
            [root]
            if root.is_file()
            else (root.rglob("*") if self.config.get("recursive") else root.iterdir())
        )
        paths = sorted(
            path
            for path in candidates
            if path.is_file() and path.suffix.lower() == ".pdf"
        )
        worker = DocumentWorker(
            config=ExtractionConfig(**self.config.get("extraction", {})),
            run_id=self.state.run_id,
            redis_client=self.sync,
        )
        for path in paths:
            path_id = hashlib.sha256(str(path).encode()).hexdigest()
            journal = self.key("source:" + path_id)
            saved = await self.client.get(journal)
            if saved:
                manifest = await self.manifest(decoded(saved))
            else:
                while await self.client.scard(self.key("active")) >= self.config.get(
                    "manifest_capacity", 50
                ):
                    await self.pause()
                result = await self.blocking(
                    worker.process_pdf, path, database_engine=self.engine, publish=False
                )
                manifest = asdict(result)
                key = manifest["manifest_key"]
                async with self.client.pipeline(transaction=True) as pipe:
                    pipe.set(key, json.dumps(manifest, allow_nan=False))
                    pipe.set(journal, key)
                    pipe.rpush(self.key("manifests"), key)
                    if manifest["status"] == "completed" and manifest_images(manifest):
                        pipe.sadd(self.key("active"), key)
                    await pipe.execute()
            key = manifest["manifest_key"]
            if manifest["status"] == "completed" and manifest_images(manifest):
                await self.emit(
                    "docling.pdf_queued",
                    document_attempt_id=key,
                    image_keys=[
                        image["redis_key"] for image in manifest_images(manifest)
                    ],
                )
                if (
                    await self.client.scard(self.key("active"))
                    >= self.state.fill_threshold
                ):
                    await self.emit("docling.filled")
            else:
                outcome = (
                    "no_assets"
                    if manifest["status"]
                    in {"completed", "no_assets", "already_stored"}
                    else "failed"
                )
                await self.emit(
                    "docling.pdf_finished",
                    document_attempt_id=key,
                    outcome=outcome,
                    error="; ".join(manifest.get("errors", [])) or None,
                )
        # Completion event must exist before the marker skips producer recovery.
        await self.emit("docling.completed")
        await self.client.set(self.key("producer_done"), "1")
        self.wakeup.set()

    async def enable_models(self):
        """Latch the one-time 20-manifest gate, including a smaller final batch."""
        await self.client.set(self.key("models_enabled"), "1")
        self.wakeup.set()

    async def route_manifests(self):
        """Route one manifest at a time, publishing claims before image jobs."""
        while not self.closed:
            if not await self.client.exists(self.key("models_enabled")):
                await self.pause()
                continue
            for key in await self.manifests():
                if await self.client.sismember(self.key("routed"), key):
                    continue
                manifest = await self.manifest(key)
                if manifest["status"] == "completed" and manifest_images(manifest):
                    # A saved manifest may precede its publication event after a crash.
                    await self.emit(
                        "docling.pdf_queued",
                        document_attempt_id=key,
                        image_keys=[
                            image["redis_key"] for image in manifest_images(manifest)
                        ],
                    )
                    await self.emit("vlm.pdf_claimed", document_attempt_id=key)
                    for asset in manifest["assets"]:
                        for image in asset.get("images_meta", []):
                            image_key = image["redis_key"]
                            model = image.get("vlm_key") or await route_image(
                                self.state, image, asset.get("context") or {}
                            )

                            def set_model(current, selected=model):
                                current["vlm_key"] = selected

                            if not image.get("vlm_key"):
                                await self.blocking(
                                    update_manifest_image,
                                    self.sync,
                                    key,
                                    image_key,
                                    set_model,
                                )
                            await self.enqueue_once(
                                self.state.vlm_queues[model],
                                "vlm:" + image_key,
                                {
                                    "manifest_key": key,
                                    "image_key": image_key,
                                    "model": model,
                                    "stage": "vlm",
                                },
                            )
                await self.client.sadd(self.key("routed"), key)
            await self.pause()

    async def queue_format(self, event):
        """Route saved raw output and context to exactly one formatter queue."""
        key, image_key = event.document_attempt_id, event.image_key
        manifest = await self.manifest(key)
        image = find_manifest_image(manifest, image_key)
        context = self.image_context(manifest, image_key)
        model = image.get("formatter_key") or await route_result(
            self.state, image["vlm_key"], image["vlm_result"]["raw_output"], context
        )
        if not image.get("formatter_key"):

            def select(current):
                current["formatter_key"] = model

            await self.blocking(
                update_manifest_image, self.sync, key, image_key, select
            )
        await self.enqueue_once(
            self.state.formatter_queues[model],
            "formatter:" + image_key,
            {
                "manifest_key": key,
                "image_key": image_key,
                "model": model,
                "stage": "formatter",
            },
        )

    @staticmethod
    def image_context(manifest, image_key):
        """Find caption, mentions and nearby text for a particular crop."""
        return next(
            (
                asset.get("context") or {}
                for asset in manifest["assets"]
                if any(
                    image["redis_key"] == image_key
                    for image in asset.get("images_meta", [])
                )
            ),
            {},
        )

    async def queue_has_work(self, queue):
        """Count unacknowledged work rather than historical stream length."""
        groups = await self.client.xinfo_groups(queue)
        for group in groups:
            if decoded(group.get("name", group.get(b"name"))) == WORKER_GROUP:
                if group.get("pending", group.get(b"pending", 0)):
                    return True
                last = decoded(
                    group.get("last-delivered-id", group.get(b"last-delivered-id"))
                )
                return bool(
                    await self.client.xrange(queue, min=f"({last}", max="+", count=1)
                )
        return False

    async def consume_models(self):
        """Drain compatible model groups with fair, bounded loading intervals."""
        servers = ModelServers(self.config["models"])
        while not self.closed:
            worked = False
            for group in self.state.resource_plan:
                queues = [
                    (key, queue)
                    for key in group
                    for mapping in (self.state.vlm_queues, self.state.formatter_queues)
                    if (queue := mapping.get(key))
                ]
                active = [
                    (key, queue)
                    for key, queue in queues
                    if await self.queue_has_work(queue)
                ]
                if not active:
                    continue
                worked = True
                # Recheck memory before starting a new group, after extraction or
                # other applications may have changed available capacity.
                if any(
                    self.config["models"][key].get("serving") == "managed"
                    for key, _ in active
                ):
                    from hardware import inspect_hardware

                    hardware = await self.blocking(inspect_hardware)
                    if (
                        len(
                            compatible_groups(
                                self.config, hardware, {key for key, _ in active}
                            )
                        )
                        != 1
                    ):
                        raise RuntimeError(
                            "Available memory no longer fits the scheduled model group"
                        )
                async with servers.loaded(sorted({key for key, _ in active})):
                    await run_together(
                        self.drain_model(key, queue) for key, queue in active
                    )
            if not worked:
                await self.pause()

    async def drain_model(self, model, queue):
        """Bound requests per model and retry pending jobs before taking new ones."""
        limit = self.config["models"][model].get("requests_per_model", 1)
        batch = self.config.get("model_batch_size", 50)

        async def consumer(slot, count):
            for _ in range(count):
                job = await self.claim(queue, WORKER_GROUP, f"worker-{slot}")
                if job is None:
                    return
                entry_id, payload = job
                async with self.model_limits[model]:
                    await self.process_image(payload)
                await self.client.xack(queue, WORKER_GROUP, entry_id)

        await run_together(
            consumer(slot, batch // limit + (slot < batch % limit))
            for slot in range(min(limit, batch))
        )

    async def process_image(self, job):
        """Retry one image at the head of its consumer queue and retain failures."""
        key, image_key, stage = job["manifest_key"], job["image_key"], job["stage"]
        status = "vlm_status" if stage == "vlm" else "format_status"
        counter = "vlm_retry_count" if stage == "vlm" else "format_retry_count"
        while True:
            manifest = await self.manifest(key)
            image = find_manifest_image(manifest, image_key)
            if image.get("failed"):
                await self.emit(
                    "worker.item_failed",
                    source=stage,
                    document_attempt_id=key,
                    image_key=image_key,
                    error=image.get("error", "Image failed"),
                )
                return
            if image.get(status) == "complete":
                await self.image_completed(job, image)
                return
            model = self.config["models"][job["model"]]

            def begin(current):
                current[status] = "processing"

            await self.blocking(update_manifest_image, self.sync, key, image_key, begin)
            try:
                context = self.image_context(manifest, image_key)
                if stage == "vlm":
                    png = await self.client.hget(image_key, "png")
                    if not png:
                        raise ValueError("Image bytes are missing")
                    raw = await inference.extract(model, png, context)

                    def complete(current):
                        current[status] = "complete"
                        current["vlm_result"] = {
                            "raw_output": raw,
                            "model": model["model_id"],
                        }
                        current.pop("error", None)
                else:
                    schema_key = self.config.get("schema_matcher", {}).get(
                        image.get("class_name"), self.config["fallback_schema"]
                    )
                    schema = self.context["schema"]["formatter_schemas"][schema_key]
                    result = await inference.format_result(
                        model,
                        image["vlm_result"]["raw_output"],
                        schema,
                        context,
                        image.get("error"),
                    )
                    result.update(
                        schema_key=schema_key, schema_id=self.context["schema_id"]
                    )
                    candidate = deepcopy(manifest)
                    for asset in candidate["assets"]:
                        asset["images_meta"] = [
                            item
                            for item in asset.get("images_meta", [])
                            if item["redis_key"] == image_key
                        ]
                    candidate_image = find_manifest_image(candidate, image_key)
                    candidate_image.update(formatting=result, format_status="complete")
                    document_rows(candidate, run_context=self.context)

                    def complete(current):
                        current[status] = "complete"
                        current["formatting"] = result
                        current.pop("error", None)

                await self.blocking(
                    update_manifest_image, self.sync, key, image_key, complete
                )
            except Exception as exc:

                def fail(current, error=exc):
                    current[status] = "failed"
                    current["error"] = f"{type(error).__name__}: {error}"
                    current.setdefault("attempt_errors", []).append(
                        {
                            "stage": stage,
                            "error": current["error"],
                            "response": getattr(error, "response", None),
                        }
                    )
                    retries = current.get(counter, 0)
                    if retries < self.config["max_image_retries"]:
                        current[counter] = retries + 1
                    else:
                        current["failed"] = True

                image = await self.blocking(
                    update_manifest_image, self.sync, key, image_key, fail
                )
                if image.get("failed"):
                    await self.emit(
                        "worker.item_failed",
                        source=stage,
                        document_attempt_id=key,
                        image_key=image_key,
                        error=image["error"],
                    )
                    return
                # Keeping the unacknowledged job ahead of new reads implements a
                # priority requeue without breaking Redis Stream delivery order.
                await asyncio.sleep(min(0.1 * 2 ** min(image.get(counter, 0), 5), 2))

    async def image_completed(self, job, image):
        """Publish saved results without repeating successful model calls."""
        key, image_key = job["manifest_key"], job["image_key"]
        if job["stage"] == "vlm":
            await self.client.set(
                image_key + ":vlm_result", json.dumps(image["vlm_result"])
            )
            await self.emit(
                "vlm.image_completed",
                document_attempt_id=key,
                image_key=image_key,
                payload_ref=image_key + ":vlm_result",
            )
        else:
            async with self.client.pipeline(transaction=True) as pipe:
                pipe.set(image_key + ":formatting", json.dumps(image["formatting"]))
                pipe.set(
                    image_key + ":unmapped_observations",
                    json.dumps(image["formatting"]["unmapped_observations"]),
                )
                await pipe.execute()
            data = image["formatting"]["data"]
            chart_type = (
                data.get("chart_type", image.get("class_name") or "UNKNOWN")
                if isinstance(data, dict)
                else "UNKNOWN"
            )
            await self.emit(
                "analysis.completed",
                document_attempt_id=key,
                image_key=image_key,
                chart_type=chart_type,
                payload_ref=image_key + ":formatting",
                unmapped_observations_ref=image_key + ":unmapped_observations",
            )

    async def map_fields(self, event):
        """Validate one image's relational rows against the run's stored schema."""
        manifest = await self.manifest(event.document_attempt_id)
        for asset in manifest["assets"]:
            asset["images_meta"] = [
                image
                for image in asset.get("images_meta", [])
                if image["redis_key"] == event.image_key
            ]
        try:
            rows = document_rows(manifest, run_context=self.context)
        except Exception as exc:

            def fail(image, error=exc):
                image.update(
                    failed=True,
                    format_status="failed",
                    error=f"Mapping failed: {error}",
                )

            await self.blocking(
                update_manifest_image,
                self.sync,
                event.document_attempt_id,
                event.image_key,
                fail,
            )
            await self.emit(
                "worker.item_failed",
                source="formatter",
                document_attempt_id=event.document_attempt_id,
                image_key=event.image_key,
                error=f"Mapping failed: {exc}",
            )
            return
        await self.client.set(event.image_key + ":fields", json.dumps(rows))
        await self.emit(
            "db_fields.completed",
            document_attempt_id=event.document_attempt_id,
            image_key=event.image_key,
            payload_ref=event.image_key + ":fields",
        )

    async def queue_write(self, event):
        """Mark the graph's mapping stage complete before writer eligibility."""
        await self.client.sadd(self.key("mapped"), event.image_key)
        self.wakeup.set()

    async def acknowledge_pdf(self, event):
        """Persist the terminal claim outcome without discarding failure evidence."""
        await self.client.sadd(self.key("acknowledged"), event.document_attempt_id)
        self.wakeup.set()

    async def write_ready(self):
        """Commit batches of complete documents; flush the tail after upstream ends."""
        while not self.closed:
            ready, upstream = [], False
            for key in await self.manifests():
                manifest = await self.manifest(key)
                images = manifest_images(manifest)
                if manifest["status"] != "completed" or not images:
                    continue
                if await self.client.sismember(self.key("written"), key):
                    continue
                # Terminal failures must be observed by the graph before an omit
                # document can be completed with zero successful images.
                settled = all(
                    [
                        image.get("failed")
                        or await self.client.sismember(
                            self.key("mapped"), image["redis_key"]
                        )
                        for image in images
                    ]
                )
                if not settled:
                    upstream = True
                    continue
                await self.client.srem(self.key("active"), key)
                if (
                    any(image.get("failed") for image in images)
                    and not self.config["approve_with_fails"]
                ):
                    for image in images:
                        if not image.get("failed"):
                            await self.emit(
                                "worker.item_failed",
                                source="writer",
                                document_attempt_id=key,
                                image_key=image["redis_key"],
                                error="Document write blocked by a failed image",
                            )
                    await self.client.sadd(self.key("written"), key)
                    continue
                ready.append(manifest)
            size = self.config.get("writer_batch_size", 50)
            final = (
                bool(await self.client.exists(self.key("producer_done")))
                and not upstream
            )
            if ready and (len(ready) >= size or final):
                batch = ready[:size]
                # Validate each document before beginning a transaction. A mapping
                # error is a document failure and cannot partially write its rows.
                for manifest in batch:
                    document_rows(manifest, run_context=self.context)
                await self.blocking(write_batches, self.engine, batch, batch_size=size)
                for manifest in batch:
                    if manifest.get("db_status") != "complete":
                        await self.blocking(mark_document_stored, self.sync, manifest)
                    key = manifest["manifest_key"]
                    for image in manifest_images(manifest):
                        if not image.get("failed"):
                            await self.emit(
                                "writer.committed",
                                document_attempt_id=key,
                                image_key=image["redis_key"],
                                record_id=image["redis_key"],
                            )
                    await self.client.sadd(self.key("written"), key)
                self.wakeup.set()
            await self.pause()

    async def finish_writes(self):
        """Wait for documents consisting entirely of omitted failed images too."""
        if self.context["status"] == "complete":
            return
        while True:
            if self.failure:
                raise RuntimeError(
                    "Writer could not finalize the run"
                ) from self.failure
            remaining = False
            for key in await self.manifests():
                manifest = await self.manifest(key)
                if manifest["status"] == "completed" and manifest_images(manifest):
                    remaining |= not await self.client.sismember(
                        self.key("written"), key
                    )
            if not remaining:
                return
            await self.pause()

    async def record_outcome(self, outcome, errors):
        """Persist terminal status without making failed runs look successful."""
        await self.blocking(
            finish_run, self.engine, self.state.run_id, outcome=outcome, errors=errors
        )

    async def close(self):
        """Stop owned tasks and servers while retaining Redis recovery data."""
        if self.closed:
            return
        self.closed = True
        workers = [task for task in self.tasks if task.get_name() != "lease"]
        leases = [task for task in self.tasks if task.get_name() == "lease"]
        for task in workers:
            task.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        if self.threads:
            await asyncio.gather(*self.threads, return_exceptions=True)
        for task in leases:
            task.cancel()
        await asyncio.gather(*leases, return_exceptions=True)
        try:
            await self.client.eval(_RELEASE, 1, self.key("lease"), self.token)
        finally:
            await self.client.aclose()
            self.sync.close()
            self.engine.dispose()


async def get_runtime(state):
    """Restore process-local workers from durable run state on any resumed hook."""
    key = (asyncio.get_running_loop(), state.run_id)
    runtime = _RUNTIMES.get(key)
    if runtime is None or runtime.closed:
        if state.graph_managed_stages:
            from agent.stage_runtime import StageRuntime

            runtime = StageRuntime(state)
        else:
            runtime = Runtime(state)
        try:
            await runtime.start()
        except BaseException:
            await runtime.close()
            raise
        _RUNTIMES[key] = runtime
    return runtime
