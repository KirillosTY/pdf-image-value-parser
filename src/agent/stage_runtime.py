"""Keep model consumers live while visible graph nodes publish ready work.

Routing, queue publication, mapping and manifest writes have explicit graph
nodes. Queue consumers run continuously; no stage waits for an entire batch.
"""

import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone

from parser.src.db.db import document_rows, write_batches
from parser.src.redis.state import (
    find_manifest_image,
    manifest_images,
    mark_document_stored,
    update_manifest_image,
)

from agent.coordinator import refresh_progress
from agent.progress_messages import show_progress
from agent.resources import ModelServers, compatible_groups
from agent.routing import route_image, route_result
from agent.runtime import Runtime, run_together
from agent.state import DocumentState, ImageState, State, WorkerEvent


class StageRuntime(Runtime):
    """Stream image work through the graph while preserving durable queue ownership."""

    def __init__(self, state, **kwargs):
        """Share one MainAgent decision lock across both routing nodes."""
        super().__init__(state, **kwargs)
        self.main_agent_lock = asyncio.Lock()
        self.routing_tasks = {}

    def check_workers(self):
        """Surface producer, routing or consumer failure at every stage boundary."""
        if self.failure:
            raise RuntimeError("A pipeline worker failed") from self.failure

    async def active_manifest_keys(self) -> list[str]:
        """Expose unfinished manifests immediately, including the first document."""
        ready = []
        for key in await self.manifests():
            if await self.client.sismember(self.key("stages_completed"), key):
                continue
            manifest = await self.manifest(key)
            if manifest["status"] == "completed" and manifest_images(manifest):
                ready.append(key)
        if ready:
            await self.client.set(self.key("models_enabled"), "1", nx=True)
            await self.client.set(
                self.key("gate_opened_at"), datetime.now(timezone.utc).isoformat(), nx=True,
            )
        return ready[:self.config["manifest_capacity"]]

    async def await_ready_work(self):
        """Wait for a publication/completion; periodically check worker health."""
        self.check_workers()
        try:
            await asyncio.wait_for(self.wakeup.wait(), timeout=1)
        except asyncio.TimeoutError:
            pass
        self.wakeup.clear()
        self.check_workers()

    async def route_ready_images(self, state: State, *, stage: str, thinking: bool):
        """Let both routing nodes share one serialized MainAgent, without blocking workers."""
        self.check_workers()
        queues = state.vlm_queues if stage == "vlm" else state.formatter_queues
        if thinking != bool(state.config["think_sorting"] and len(queues) > 1):
            raise ValueError("Routing node does not match think_sorting")
        pending = self.routing_tasks.get(stage)
        if pending:
            if not pending.done():
                return
            pending.result()
            self.tasks.remove(pending)
            del self.routing_tasks[stage]
        field = "vlm_key" if stage == "vlm" else "formatter_key"
        for key in state.active_manifests:
            manifest = await self.manifest(key)
            for image in manifest_images(manifest):
                if image.get("failed") or image.get(field):
                    continue
                if stage == "formatter" and image.get("vlm_status") != "complete":
                    continue
                image_key = image["redis_key"]
                if thinking:
                    # At most one pending decision per routing stage; the shared
                    # FIFO lock prevents simultaneous MainAgent requests and lets
                    # completed VLM outputs compete fairly with new image routes.
                    async def route(key=key, image_key=image_key):
                        async with self.main_agent_lock:
                            await self.save_route(state, key, image_key, stage, field, queues)
                    task = asyncio.create_task(
                        self.guard("main_agent_route_" + stage, route),
                        name="main_agent_route_" + stage,
                    )
                    self.routing_tasks[stage] = task
                    self.tasks.append(task)
                    return
                await self.save_route(state, key, image_key, stage, field, queues)

    async def save_route(self, state, key, image_key, stage, field, queues):
        """Persist one validated choice; reuse it after interruption."""
        manifest = await self.manifest(key)
        image = find_manifest_image(manifest, image_key)
        if image.get("failed") or image.get(field):
            return
        context = self.image_context(manifest, image_key)
        label = "VLM" if stage == "vlm" else "formatter"
        page = image.get("page_number")
        image_label = f"an image on page {page}" if type(page) is int else "an image"
        thinking = self.config["think_sorting"] and len(queues) > 1
        if thinking:
            show_progress(self.config, f"MainAgent: choosing a {label} for {image_label}.")
        selected = (
            await route_image(state, image, context) if stage == "vlm"
            else await route_result(state, image["vlm_key"], image["vlm_result"]["raw_output"], context)
        )
        if selected not in queues:
            raise ValueError("Routing selected a model without a prepared queue")

        def save(current):
            current[field] = selected

        await self.blocking(update_manifest_image, self.sync, key, image_key, save)
        actor = "MainAgent selected" if thinking else "Configured route selected"
        show_progress(self.config, f"{actor} {selected} for {image_label} ({label} stage).")
        self.wakeup.set()

    async def enqueue_ready_images(self, state: State, stage: str):
        """Publish ready jobs and return immediately; live consumers perform inference."""
        self.check_workers()
        queues = state.vlm_queues if stage == "vlm" else state.formatter_queues
        field = "vlm_key" if stage == "vlm" else "formatter_key"
        status = "vlm_status" if stage == "vlm" else "format_status"
        for key in state.active_manifests:
            for image in manifest_images(await self.manifest(key)):
                selected = image.get(field)
                if image.get("failed") or not selected or image.get(status) == "complete":
                    continue
                if stage == "formatter" and image.get("vlm_status") != "complete":
                    continue
                if stage == "vlm":
                    await self.client.sadd(self.key("claimed_documents"), key)
                await self.enqueue_once(
                    queues[selected], stage + ":" + image["redis_key"],
                    {"manifest_key": key, "image_key": image["redis_key"],
                     "model": selected, "stage": stage},
                )

    async def consume_models(self):
        """Keep external formatter/VLM consumers live; time-share managed resources."""
        queues = [*self.state.vlm_queues.items(), *self.state.formatter_queues.items()]
        external = [(key, queue) for key, queue in queues
                    if self.config["models"][key]["serving"] == "external"]
        managed = [(key, queue) for key, queue in queues
                   if self.config["models"][key]["serving"] == "managed"]

        async def consume_external(model, queue):
            while not self.closed:
                await self.drain_model(model, queue)
                await self.pause()

        workers = [consume_external(key, queue) for key, queue in external]
        if managed:
            workers.append(self.consume_managed(managed))
        await run_together(workers)

    async def consume_managed(self, queues):
        """Give compatible managed groups short turns without draining a whole VLM batch."""
        from hardware import inspect_hardware

        servers = ModelServers(self.config["models"])
        while not self.closed:
            worked = False
            for group in self.state.resource_plan:
                active = [(key, queue) for key, queue in queues if key in group
                          and await self.queue_has_work(queue)]
                if not active:
                    continue
                worked = True
                keys = {key for key, _ in active}
                hardware = await self.blocking(inspect_hardware)
                if len(compatible_groups(self.config, hardware, keys)) != 1:
                    raise RuntimeError("Current memory cannot fit the scheduled model group")
                async with servers.loaded(sorted(keys)):
                    await run_together(
                        self.drain_model(key, queue, max_jobs=1) for key, queue in active
                    )
            if not worked:
                await self.pause()

    def image_event(self, state, manifest_key, image_key):
        """Identify an image for the durable mapping/write adapters."""
        return WorkerEvent(
            event_id="graph:" + image_key, run_id=state.run_id,
            kind="analysis.completed", source="formatter",
            timestamp=datetime.now(timezone.utc).isoformat(),
            document_attempt_id=manifest_key, image_key=image_key,
        )

    async def map_ready_images(self, state: State):
        """Map each completed formatter result without waiting for its siblings."""
        self.check_workers()
        for key in state.active_manifests:
            for image in manifest_images(await self.manifest(key)):
                if image.get("failed") or image.get("format_status") != "complete":
                    continue
                if await self.client.sismember(self.key("mapped"), image["redis_key"]):
                    continue
                event = self.image_event(state, key, image["redis_key"])
                await self.map_fields(event)
                saved = find_manifest_image(await self.manifest(key), image["redis_key"])
                if not saved.get("failed"):
                    await self.queue_write(event)

    async def write_documents(self, state: State):
        """Write each settled manifest atomically; block failed manifests and continue."""
        self.check_workers()
        for key in await self.manifests():
            if await self.client.sismember(self.key("written"), key):
                continue
            manifest = await self.manifest(key)
            images = manifest_images(manifest)
            if manifest["status"] != "completed" or not images:
                continue
            settled = all([
                image.get("failed") or await self.client.sismember(self.key("mapped"), image["redis_key"])
                for image in images
            ])
            if not settled:
                continue
            if any(image.get("failed") for image in images) and not state.config["approve_with_fails"]:
                await self.client.sadd(self.key("write_blocked"), key)
                show_progress(self.config, "Database write blocked: a document has failed images and the run requires all images to succeed.")
            else:
                # A manifest is the transaction boundary. Never hold one completed
                # document waiting for unrelated documents to finish.
                document_rows(manifest, run_context=self.context)
                await self.blocking(write_batches, self.engine, [manifest], batch_size=1)
                if manifest.get("db_status") != "complete":
                    await self.blocking(mark_document_stored, self.sync, manifest)
                show_progress(self.config, "Database: saved one complete document.")
                for image in images:
                    if not image.get("failed"):
                        await self.emit(
                            "writer.committed", document_attempt_id=key,
                            image_key=image["redis_key"], record_id=image["redis_key"],
                        )
            async with self.client.pipeline(transaction=True) as pipe:
                pipe.sadd(self.key("written"), key)
                pipe.sadd(self.key("stages_completed"), key)
                pipe.srem(self.key("active"), key)
                await pipe.execute()
            self.wakeup.set()

    async def is_finished(self):
        """Require producer completion and a written or explicitly blocked outcome."""
        self.check_workers()
        if not await self.client.exists(self.key("producer_done")):
            return False
        for key in await self.manifests():
            manifest = await self.manifest(key)
            if manifest["status"] == "completed" and manifest_images(manifest):
                if not await self.client.sismember(self.key("written"), key):
                    return False
        # A worker saves its result before publishing references and acknowledging
        # the job. Let that final bookkeeping finish before closing consumers.
        if any(not task.done() for task in self.routing_tasks.values()):
            return False
        for queue in [*self.state.vlm_queues.values(), *self.state.formatter_queues.values()]:
            if await self.queue_has_work(queue):
                return False
        return True

    async def finish_writes(self):
        """Never label a partially processed run complete."""
        if not await self.is_finished():
            raise RuntimeError("Cannot finish before every manifest is settled")

    async def progress(self, state: State) -> dict:
        """Expose real per-image stage results in Studio using saved Redis records."""
        current = deepcopy(state)
        production_finished = bool(await self.client.exists(self.key("producer_done")))
        current.images, current.documents = {}, {}
        current.docling = replace(state.docling, processed_pdfs=0, queued_pdfs=0,
                                  failed_pdfs=0, no_assets_pdfs=0, terminal_pdfs={})
        current.vlm = replace(state.vlm, processed_images=0, failed_images=0)
        current.formatter = replace(state.formatter, processed_images=0, failed_images=0)
        errors = list(state.errors)
        for key in await self.manifests():
            manifest = await self.manifest(key)
            images = manifest_images(manifest)
            current.docling.processed_pdfs += 1
            errors.extend(manifest.get("errors", []))
            if manifest["status"] != "completed" or not images:
                failed = manifest["status"] not in {"completed", "no_assets", "already_stored"}
                current.docling.terminal_pdfs[key] = "failed" if failed else "no_assets"
                if failed:
                    current.docling.failed_pdfs += 1
                    errors.append(f"Docling failed: {key}")
                else:
                    current.docling.no_assets_pdfs += 1
                continue
            current.docling.queued_pdfs += 1
            current.docling.terminal_pdfs[key] = "queued"
            claimed = bool(await self.client.sismember(self.key("claimed_documents"), key))
            current.documents[key] = DocumentState([image["redis_key"] for image in images], claimed)
            blocked = bool(await self.client.sismember(self.key("write_blocked"), key))
            written = bool(await self.client.sismember(self.key("written"), key)) and not blocked
            if blocked:
                errors.append(f"Document write blocked by a failed image: {key}")
            for image in images:
                image_key = image["redis_key"]
                extracted = image.get("vlm_status") == "complete"
                formatted = image.get("format_status") == "complete"
                mapped = bool(await self.client.sismember(self.key("mapped"), image_key))
                failed = bool(image.get("failed"))
                stage = ("failed" if failed or blocked else "stored" if written
                         else "writing" if mapped else "mapping" if formatted
                         else "analysis" if extracted else "vlm" if claimed else "queued")
                error = image.get("error") if failed else None
                if error:
                    errors.append(error)
                current.images[image_key] = ImageState(
                    stage=stage, chart_type=image.get("class_name"), error=error,
                    vlm_model=image.get("vlm_key"), formatter_model=image.get("formatter_key"),
                    result_ref=image_key + ":vlm_result" if extracted else None,
                    normalized_ref=image_key + ":formatting" if formatted else None,
                    fields_ref=image_key + ":fields" if mapped else None,
                    unmapped_observations_ref=image_key + ":unmapped_observations" if formatted else None,
                    analysis_status="completed" if formatted else "failed" if failed else "pending",
                    storage_status="failed" if failed or blocked else "stored" if written
                    else "writing" if mapped else "mapping" if formatted else "pending",
                    record_id=image_key if written and not failed else None,
                )
                current.vlm.processed_images += int(extracted)
                current.vlm.failed_images += int(failed and not extracted)
                current.formatter.processed_images += int(formatted)
                current.formatter.failed_images += int(failed and extracted and not formatted)
        gate = await self.client.get(self.key("gate_opened_at"))
        threshold = await self.client.get(self.key("threshold_reached_at"))
        if threshold:
            current.docling.filled_at = threshold.decode() if isinstance(threshold, bytes) else threshold
        if production_finished:
            current.docling.status = "completed"
            current.docling.completed_at = state.docling.completed_at or datetime.now(timezone.utc).isoformat()
        elif gate:
            current.docling.status = "filled"
        else:
            current.docling.status = "running"
        current.errors = list(dict.fromkeys(errors))
        refresh_progress(current)
        return {name: getattr(current, name) for name in (
            "images", "documents", "docling", "vlm", "formatter", "queues", "errors",
        )}
