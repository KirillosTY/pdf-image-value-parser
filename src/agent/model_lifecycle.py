"""Release local model residency at every sequential batch stage boundary."""

import asyncio
import json
from contextlib import asynccontextmanager
from urllib.request import Request, urlopen

from agent.resources import ModelServers, compatible_groups, endpoint_options
from hardware import inspect_hardware


class BatchModelLifecycle:
    """Manage only configured models on explicitly supported local endpoints."""

    def __init__(self, config):
        """Describe endpoint ownership without contacting any service."""
        self.config = config
        self.models = config["models"]
        self.endpoints = {}
        for key, model in self.models.items():
            if model.get("serving") != "external":
                continue
            needed = bool(set(model["roles"]) & {"vlm", "formatter"}) or (
                key == config.get("main_agent")
            )
            if not needed and model.get("lifecycle") != "ollama":
                continue
            if model.get("lifecycle") != "ollama":
                raise ValueError(f"Batch processing requires Ollama lifecycle control: {key}")
            options = endpoint_options(model)
            base = options["base_url"].rstrip("/")
            if base.endswith("/v1"):
                base = base[:-3]
            endpoint = self.endpoints.setdefault(base, {"options": options, "models": set()})
            endpoint["models"].add(model["model_id"])

    async def request(self, base, path, payload=None):
        """Use Ollama's native lifecycle API alongside OpenAI-compatible inference."""
        options = self.endpoints[base]["options"]

        def send():
            headers = {"Content-Type": "application/json"}
            if options["api_key"] != "EMPTY":
                headers["Authorization"] = "Bearer " + options["api_key"]
            request = Request(
                base + path,
                data=None if payload is None else json.dumps(payload).encode(),
                headers=headers,
            )
            with urlopen(request, timeout=30) as response:
                return json.load(response)

        # Do not leave an unload request racing another stage after cancellation.
        task = asyncio.create_task(asyncio.to_thread(send))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise

    async def clear(self):
        """Unload approved resident models and confirm the endpoints are empty."""
        for base, endpoint in self.endpoints.items():
            running = (await self.request(base, "/api/ps"))["models"]
            names = {entry.get("name") or entry["model"] for entry in running}
            unknown = names - endpoint["models"]
            if unknown:
                raise RuntimeError(
                    "Batch processing needs exclusive use of its model endpoint; "
                    f"unrelated resident models were left untouched: {sorted(unknown)}"
                )
            for name in sorted(names):
                await self.request(base, "/api/generate", {
                    "model": name, "keep_alive": 0, "stream": False,
                })
            for _ in range(60):
                if not (await self.request(base, "/api/ps"))["models"]:
                    break
                await asyncio.sleep(0.5)
            else:
                raise RuntimeError("Ollama did not release model residency; refusing to advance")

    async def check_memory(self, key=None):
        """Check measured free memory and enforce every known per-model estimate."""
        hardware = await asyncio.to_thread(inspect_hardware)
        reserve = self.config.get("memory_reserve_gib", 2)
        ram = hardware.get("ram_available_gib")
        if ram is None or ram <= reserve:
            raise RuntimeError("Insufficient available RAM above the configured reserve")
        if key is None:
            return
        model = self.models[key]
        if model.get("serving") == "managed":
            compatible_groups(self.config, hardware, {key})
            return
        estimated_ram = model.get("estimated_ram_gib")
        if estimated_ram is not None and estimated_ram + reserve > ram:
            raise RuntimeError(f"Available RAM cannot fit {key}")
        estimated_vram = model.get("estimated_vram_gib")
        if estimated_vram is not None and model.get("device") != "cpu":
            gpus = hardware.get("gpus", [])
            index = model.get("gpu_index", 0)
            free = gpus[index].get("vram_available_gib") if index < len(gpus) else None
            if free is None or estimated_vram + reserve > free:
                raise RuntimeError(f"Available VRAM cannot fit {key}")

    @asynccontextmanager
    async def loaded(self, key):
        """Keep one selected model resident until its entire stage turn finishes."""
        await self.clear()
        await self.check_memory(key)
        try:
            async with ModelServers(self.models).loaded([key]):
                # External Ollama models load lazily on their first inference call.
                yield
        finally:
            await self.clear()
