"""Execute the visible graph stages using durable per-model Redis queues.

Only Docling and the ownership lease run in the background. Routing, model
requests, mapping and SQL writes execute inside their corresponding graph nodes.
"""

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
from agent.resources import ModelServers, compatible_groups
from agent.routing import route_image, route_result
from agent.runtime import Runtime, run_together
from agent.state import DocumentState, ImageState, State, WorkerEvent


class StageRuntime(Runtime):
    """Let graph nodes own each processing stage while Docling fills the buffer."""

    def check_workers(self):
        """Surface producer or lease failure at every stage boundary."""
        if self.failure:
            raise RuntimeError("A pipeline worker failed") from self.failure

    async def next_manifest_batch(self, state: State) -> list[str]:
        """Wait for the initial minimum, then return at most the manifest capacity."""
        while True:
            self.check_workers()
            done = bool(await self.client.exists(self.key("producer_done")))
            ready = []
            for key in await self.manifests():
                if await self.client.sismember(self.key("stages_completed"), key):
                    continue
                manifest = await self.manifest(key)
                if manifest["status"] == "completed" and manifest_images(manifest):
                    ready.append(key)
            enabled = bool(await self.client.exists(self.key("models_enabled")))
            if done or (ready and (enabled or len(ready) >= state.fill_threshold)):
                if ready and not enabled:
                    await self.enable_models()
                    await self.client.set(
                        self.key("gate_opened_at"),
                        datetime.now(timezone.utc).isoformat(), nx=True,
                    )
                    if len(ready) >= state.fill_threshold:
                        await self.client.set(
                            self.key("threshold_reached_at"),
                            datetime.now(timezone.utc).isoformat(), nx=True,
                        )
                return ready[:state.config["manifest_capacity"]]
            await self.pause()

    async def route_batch(self, state: State, *, stage: str, thinking: bool) -> None:
        """Save each image's model choice inside its visible routing node."""
        self.check_workers()
        configured = state.config[f"{'vlm' if stage == 'vlm' else 'formatter'}_think_sorting"]
        queues = state.vlm_queues if stage == "vlm" else state.formatter_queues
        if thinking != bool(configured and len(queues) > 1):
            raise ValueError("Routing node does not match this stage's thinking flag")
        field = "vlm_key" if stage == "vlm" else "formatter_key"
        for key in state.manifest_batch:
            manifest = await self.manifest(key)
            for image in manifest_images(manifest):
                self.check_workers()
                if image.get("failed") or image.get(field):
                    continue
                context = self.image_context(manifest, image["redis_key"])
                selected = (
                    await route_image(state, image, context)
                    if stage == "vlm"
                    else await route_result(
                        state, image["vlm_key"], image["vlm_result"]["raw_output"], context,
                    )
                )
                if selected not in queues:
                    raise ValueError("Routing selected a model without a prepared queue")

                def save(current, model=selected):
                    current[field] = model

                await self.blocking(
                    update_manifest_image, self.sync, key, image["redis_key"], save,
                )

    async def run_model_stage(self, state: State, stage: str) -> None:
        """Enqueue this batch and finish its actual inference before leaving the node."""
        self.check_workers()
        queues = state.vlm_queues if stage == "vlm" else state.formatter_queues
        field = "vlm_key" if stage == "vlm" else "formatter_key"
        status = "vlm_status" if stage == "vlm" else "format_status"
        for key in state.manifest_batch:
            manifest = await self.manifest(key)
            if stage == "vlm":
                await self.client.sadd(self.key("claimed_documents"), key)
            for image in manifest_images(manifest):
                if image.get("failed"):
                    continue
                selected = image[field]
                await self.enqueue_once(
                    queues[selected], stage + ":" + image["redis_key"],
                    {"manifest_key": key, "image_key": image["redis_key"],
                     "model": selected, "stage": stage},
                )
        servers = ModelServers(state.config["models"])
        while True:
            worked = False
            for group in state.resource_plan:
                self.check_workers()
                active = [
                    (key, queues[key]) for key in group if key in queues
                    and await self.queue_has_work(queues[key])
                ]
                if not active:
                    continue
                worked = True
                keys = {key for key, _ in active}
                if any(state.config["models"][key]["serving"] == "managed" for key in keys):
                    from hardware import inspect_hardware

                    hardware = await self.blocking(inspect_hardware)
                    if len(compatible_groups(state.config, hardware, keys)) != 1:
                        raise RuntimeError("Current memory cannot fit the scheduled model group")
                async with servers.loaded(sorted(keys)):
                    await run_together(self.drain_model(key, queue) for key, queue in active)
            if not worked:
                break
        self.check_workers()
        for key in state.manifest_batch:
            for image in manifest_images(await self.manifest(key)):
                if not image.get("failed") and image.get(status) != "complete":
                    raise RuntimeError(f"{stage} queue drained before its image completed")

    def image_event(self, state, manifest_key, image_key):
        """Identify the saved image for the existing mapping/write adapters."""
        return WorkerEvent(
            event_id="graph:" + image_key, run_id=state.run_id,
            kind="analysis.completed", source="formatter",
            timestamp=datetime.now(timezone.utc).isoformat(),
            document_attempt_id=manifest_key, image_key=image_key,
        )

    async def map_batch(self, state: State) -> None:
        """Validate and save relational fields for each successfully formatted image."""
        self.check_workers()
        for key in state.manifest_batch:
            manifest = await self.manifest(key)
            for image in manifest_images(manifest):
                if image.get("failed"):
                    continue
                if image.get("format_status") != "complete":
                    raise ValueError("Database mapping requires completed formatting")
                event = self.image_event(state, key, image["redis_key"])
                if not await self.client.sismember(self.key("mapped"), image["redis_key"]):
                    await self.map_fields(event)
                    saved = find_manifest_image(await self.manifest(key), image["redis_key"])
                    if not saved.get("failed"):
                        await self.queue_write(event)

    async def write_documents(self, state: State) -> None:
        """Release mapped manifests and perform eligible SQL batches inside this node."""
        self.check_workers()
        for key in state.manifest_batch:
            manifest = await self.manifest(key)
            for image in manifest_images(manifest):
                if not image.get("failed") and not await self.client.sismember(
                    self.key("mapped"), image["redis_key"],
                ):
                    raise ValueError("A document cannot leave the buffer before mapping completes")
            async with self.client.pipeline(transaction=True) as pipe:
                pipe.sadd(self.key("stages_completed"), key)
                pipe.srem(self.key("active"), key)
                await pipe.execute()
        self.wakeup.set()
        production_finished = bool(await self.client.exists(self.key("producer_done")))
        ready, upstream = [], False
        for key in await self.manifests():
            manifest = await self.manifest(key)
            images = manifest_images(manifest)
            if manifest["status"] != "completed" or not images:
                continue
            if await self.client.sismember(self.key("written"), key):
                continue
            if not await self.client.sismember(self.key("stages_completed"), key):
                upstream = True
                continue
            if any(image.get("failed") for image in images) and not state.config["approve_with_fails"]:
                async with self.client.pipeline(transaction=True) as pipe:
                    pipe.sadd(self.key("write_blocked"), key)
                    pipe.sadd(self.key("written"), key)
                    await pipe.execute()
                continue
            ready.append(manifest)
        final = production_finished and not upstream
        size = state.config["writer_batch_size"]
        while ready and (len(ready) >= size or final):
            self.check_workers()
            batch, ready = ready[:size], ready[size:]
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
                            "writer.committed", document_attempt_id=key,
                            image_key=image["redis_key"], record_id=image["redis_key"],
                        )
                await self.client.sadd(self.key("written"), key)

    async def finish_writes(self):
        """Require the visible database node to have settled every document."""
        self.check_workers()
        if not await self.client.exists(self.key("producer_done")):
            raise RuntimeError("Cannot finish while Docling is still producing documents")
        for key in await self.manifests():
            manifest = await self.manifest(key)
            if manifest["status"] == "completed" and manifest_images(manifest):
                if not await self.client.sismember(self.key("written"), key):
                    raise RuntimeError("The database node has not settled every document")

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
