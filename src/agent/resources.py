"""Plan compatible model groups and manage only explicitly owned servers."""

import asyncio
import os
import signal
from contextlib import asynccontextmanager

from openai import AsyncOpenAI


def compatible_groups(
    config: dict, hardware: dict, model_keys: set[str]
) -> list[list[str]]:
    """Pack managed models into conservative RAM and single-device VRAM budgets."""
    models = config["models"]
    reserve = config.get("memory_reserve_gib", 2.0)
    ram = hardware.get("ram_available_gib")
    gpus = [(g.get("vram_available_gib") or 0) for g in hardware.get("gpus", [])]
    managed = [key for key in model_keys if models[key].get("serving") == "managed"]
    if managed and ram is None:
        raise ValueError("Current available RAM is required for managed models")
    budget = [max(0, (ram or 0) - reserve), *[max(0, gpu - reserve) for gpu in gpus]]

    # Free-memory observations already include an externally served MainAgent.
    # reserve also covers the extractor and per-request overhead; model estimates
    # must cover the configured request concurrency, not just weight storage.
    def cost(key):
        model = models[key]
        if model.get("serving") != "managed":
            return [0] * len(budget)
        memory = model.get("estimated_ram_gib")
        device = model.get("device", "auto")
        if device not in {"cpu", "cuda", "auto"}:
            raise ValueError(f"Unsupported managed device for {key}: {device}")
        vram = 0 if device == "cpu" else model.get("estimated_vram_gib")
        if memory is None or vram is None:
            raise ValueError(f"Verified RAM/VRAM estimates are required for {key}")
        costs = [memory] + [0] * len(gpus)
        if device != "cpu":
            index = model.get("gpu_index", 0)
            if index >= len(gpus):
                raise ValueError(f"Configured GPU is unavailable for {key}")
            costs[index + 1] = vram
        if any(needed > available for needed, available in zip(costs, budget)):
            raise ValueError(f"Model {key} does not fit the current memory budget")
        return costs

    groups, usage = [], []
    for key in sorted(model_keys):
        costs = cost(key)
        if config.get("batch_processing"):
            groups.append([key])
            usage.append(costs)
            continue
        for group, used in zip(groups, usage):
            if all(
                current + needed <= available
                for current, needed, available in zip(used, costs, budget)
            ):
                group.append(key)
                used[:] = [current + needed for current, needed in zip(used, costs)]
                break
        else:
            groups.append([key])
            usage.append(costs)
    return groups


def validate_resource_plan(config: dict, hardware: dict, model_keys: set[str], groups) -> None:
    """Require exact model coverage and enforce memory limits on every group."""
    if not isinstance(groups, list) or not groups or any(
        not isinstance(group, list) or not group
        or any(not isinstance(key, str) for key in group)
        for group in groups
    ):
        raise ValueError("Resource groups must be nonempty lists of model keys")
    keys = [key for group in groups for key in group]
    if len(keys) != len(set(keys)) or set(keys) != model_keys:
        raise ValueError("Resource groups must include each queued model exactly once")
    for group in groups:
        if len(compatible_groups(config, hardware, set(group))) != 1:
            raise ValueError("A proposed model group exceeds the available memory budget")


def endpoint_options(model: dict) -> dict:
    """Resolve credentials only when creating a model client."""
    endpoint = model.get("endpoint_env")
    if not endpoint or not os.environ.get(endpoint):
        raise ValueError("Each selected model requires a configured endpoint_env")
    key = model.get("api_key_env")
    return {
        "base_url": os.environ[endpoint],
        "api_key": os.environ[key] if key else "EMPTY",
        "timeout": 120,
        "max_retries": 0,
    }


class ModelServers:
    """Launch configured argument lists and terminate only owned process groups."""

    def __init__(self, models):
        """Keep the approved registry without starting any servers."""
        self.models = models

    @asynccontextmanager
    async def loaded(self, keys):
        """Keep a compatible group loaded for its queue-draining interval."""
        processes = []
        try:
            for key in keys:
                model = self.models[key]
                if model.get("serving") == "managed":
                    environment = dict(os.environ)
                    environment["CUDA_VISIBLE_DEVICES"] = (
                        ""
                        if model.get("device") == "cpu"
                        else str(model.get("gpu_index", 0))
                    )
                    process = await asyncio.create_subprocess_exec(
                        *model["launch_command"],
                        start_new_session=True,
                        env=environment,
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                    processes.append(process)
                    async with AsyncOpenAI(**endpoint_options(model)) as client:
                        for _ in range(120):
                            if process.returncode is not None:
                                raise RuntimeError(
                                    f"Managed model {key} exited at startup"
                                )
                            try:
                                available = await asyncio.wait_for(
                                    client.models.list(), timeout=2
                                )
                                if any(
                                    item.id == model["model_id"]
                                    for item in available.data
                                ):
                                    break
                                await asyncio.sleep(1)
                            except (Exception, asyncio.TimeoutError):
                                await asyncio.sleep(1)
                        else:
                            raise TimeoutError(
                                f"Managed model {key} did not become ready"
                            )
            yield
        finally:
            for process in reversed(processes):
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
