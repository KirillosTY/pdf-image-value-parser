"""Run bounded extraction, routing and model stages with no overlapping inference."""

import asyncio
import hashlib
import json
import os
import signal
import sys
import tempfile
from pathlib import Path

from parser.src.redis.state import manifest_images

from agent.model_lifecycle import BatchModelLifecycle
from agent.progress_messages import show_progress
from agent.runtime import _LEASE, _RELEASE
from agent.stage_runtime import StageRuntime


class BatchRuntime(StageRuntime):
    """Reuse durable image queues and writes while enforcing foreground barriers."""

    def __init__(self, state, **kwargs):
        """Keep model lifecycle and endpoint leases outside graph checkpoints."""
        super().__init__(state, **kwargs)
        self.batch_models = BatchModelLifecycle(self.config)
        self.endpoint_leases = []

    async def start(self):
        """Acquire the run and endpoint leases without starting background models."""
        await super().start()
        for base in sorted(self.batch_models.endpoints):
            key = "batch_model_endpoint:" + hashlib.sha256(base.encode()).hexdigest()
            if not await self.client.set(key, self.token, nx=True, ex=60):
                raise RuntimeError("Another batch run owns this model endpoint")
            self.endpoint_leases.append(key)
        await self.batch_models.clear()

    async def heartbeat(self):
        """Renew exclusive batch endpoint ownership together with the run lease."""
        while True:
            await asyncio.sleep(10)
            for key in [self.key("lease"), *self.endpoint_leases]:
                if not await self.client.eval(_LEASE, 1, key, self.token):
                    self.failure = RuntimeError("Batch worker lost its resource lease")
                    raise self.failure

    async def release_resources(self):
        """Release endpoint ownership only after all owned stage work has stopped."""
        for key in self.endpoint_leases:
            await self.client.eval(_RELEASE, 1, key, self.token)
        self.endpoint_leases.clear()

    async def run_stage_work(self, operation):
        """Cancel in-flight batch work if the supervisor fails or loses its lease."""
        task = asyncio.create_task(operation, name="batch_stage")
        self.tasks.append(task)
        try:
            return await task
        except asyncio.CancelledError:
            self.check_workers()
            raise
        finally:
            self.tasks.remove(task)

    async def await_ready_work(self):
        """Check failures; the next Docling node fills the next batch synchronously."""
        self.check_workers()

    async def active_manifest_keys(self):
        """Resume unfinished work first, otherwise extract at most one bounded batch."""
        self.check_workers()
        active = await super().active_manifest_keys()
        if active or await self.client.exists(self.key("producer_done")):
            return active
        root = Path(self.state.input_path).expanduser().resolve()
        if not root.exists():
            raise FileNotFoundError(root)
        candidates = [root] if root.is_file() else (
            root.rglob("*") if self.config.get("recursive") else root.iterdir()
        )
        paths = sorted(path for path in candidates if path.is_file() and path.suffix.lower() == ".pdf")
        pending = []
        for path in paths:
            journal = self.key("source:" + hashlib.sha256(str(path).encode()).hexdigest())
            if not await self.client.exists(journal):
                pending.append(str(path))
        batch = pending[:self.config["manifest_capacity"]]
        if batch:
            await self.batch_models.clear()
            await self.batch_models.check_memory()
            await self.run_stage_work(self.extract_batch(batch))
            self.check_workers()
        if len(batch) == len(pending):
            await self.emit("docling.completed")
            await self.client.set(self.key("producer_done"), "1")
        return await super().active_manifest_keys()

    async def extract_batch(self, paths):
        """Wait for the extraction process to exit before allowing another model."""
        show_progress(self.config, f"Docling: extracting {len(paths)} document(s); Ollama models are unloaded.")
        payload = json.dumps({
            "paths": paths, "run_id": self.state.run_id, "prefix": self.prefix,
            "extraction": self.config.get("extraction", {}),
        }).encode()
        with tempfile.TemporaryFile() as errors:
            process = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "agent.docling_batch_worker",
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL,
                stderr=errors, start_new_session=True,
            )
            try:
                await process.communicate(payload)
                if process.returncode:
                    errors.seek(0, os.SEEK_END)
                    errors.seek(max(0, errors.tell() - 4000))
                    detail = errors.read().decode(errors="replace")
                    raise RuntimeError(f"Docling batch process failed ({process.returncode}): {detail}")
                show_progress(self.config, "Docling finished and released its process; image routing can begin.")
            finally:
                if process.returncode is None:
                    try:
                        os.killpg(process.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    try:
                        await asyncio.wait_for(process.wait(), timeout=10)
                    except asyncio.TimeoutError:
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        await process.wait()

    async def route_ready_images(self, state, *, stage, thinking):
        """Finish every pending route in this batch before any inference worker runs."""
        self.check_workers()
        queues = state.vlm_queues if stage == "vlm" else state.formatter_queues
        if thinking != bool(state.config["think_sorting"] and len(queues) > 1):
            raise ValueError("Routing node does not match think_sorting")
        field = "vlm_key" if stage == "vlm" else "formatter_key"
        jobs = []
        for key in state.active_manifests:
            for image in manifest_images(await self.manifest(key)):
                if image.get("failed") or image.get(field):
                    continue
                if stage == "formatter" and image.get("vlm_status") != "complete":
                    continue
                jobs.append((key, image["redis_key"]))

        async def route_all():
            for key, image_key in jobs:
                self.check_workers()
                await self.save_route(state, key, image_key, stage, field, queues)

        async def route_batch():
            if jobs and thinking:
                async with self.batch_models.loaded(self.config["main_agent"]):
                    await route_all()
            else:
                await route_all()

        await self.run_stage_work(route_batch())

    async def enqueue_ready_images(self, state, stage):
        """Drain each model's batch including retries, then unload before advancing."""
        await super().enqueue_ready_images(state, stage)
        queues = state.vlm_queues if stage == "vlm" else state.formatter_queues

        async def drain_stage():
            order = [key for group in state.resource_plan for key in group if key in queues]
            for key in order or sorted(queues):
                queue = queues[key]
                self.check_workers()
                if not await self.queue_has_work(queue):
                    continue
                show_progress(self.config, f"{stage.capitalize()} stage: processing the queued images with {key}.")
                async with self.batch_models.loaded(key):
                    while await self.queue_has_work(queue):
                        self.check_workers()
                        await self.drain_model(key, queue)
                show_progress(self.config, f"{key}: queue finished; model unloaded before the next stage.")

        await self.run_stage_work(drain_stage())
