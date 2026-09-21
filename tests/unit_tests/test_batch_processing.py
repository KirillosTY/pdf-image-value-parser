"""Cover sequential stages, durable batches and explicit model lifecycle control."""

import hashlib
import json
from contextlib import asynccontextmanager
from unittest.mock import Mock

import fakeredis
import fakeredis.aioredis
import pytest
from parser.src.redis.queues import create_model_queues

from agent.batch_runtime import BatchRuntime
from agent.model_lifecycle import BatchModelLifecycle
from agent.routing import match_formatter_models, match_vlm_models
from agent.state import State
from config import ModelConfig, PipelineConfig


def configuration():
    models = {}
    for key, role in [("reader_a", "vlm"), ("reader_b", "vlm"),
                      ("formatter", "formatter"), ("router", "main_agent")]:
        models[key] = ModelConfig(
            model_id=key + ":latest", roles=[role], context="Test model",
            input_description="Source", output_description="Result",
            endpoint_env="BATCH_TEST_URL", lifecycle="ollama",
            requests_per_model=4,
        )
    return PipelineConfig(
        models=models, main_agent="router", charts_targeted=["bar_chart"],
        batch_processing=True, manifest_capacity=2,
    )


def runtime_for_test(monkeypatch, **state_values):
    monkeypatch.setenv("BATCH_TEST_URL", "http://localhost:9999/v1")
    server = fakeredis.FakeServer()
    state = State(run_id="batch", config=configuration().snapshot(), **state_values)
    runtime = BatchRuntime(
        state, client=fakeredis.aioredis.FakeRedis(server=server),
        sync_client=fakeredis.FakeRedis(server=server), engine=Mock(),
    )
    return runtime, state


def test_batch_snapshot_validates_lifecycle_and_preserves_old_streaming_default():
    config = configuration()
    snapshot = config.snapshot()
    assert PipelineConfig.from_dict(snapshot).snapshot() == snapshot
    del snapshot["batch_processing"]
    assert PipelineConfig.from_dict(snapshot).batch_processing is False
    config.models["reader_a"].lifecycle = "none"
    with pytest.raises(ValueError, match="lifecycle='ollama'"):
        config.snapshot()
    config.batch_processing = "true"
    with pytest.raises(ValueError, match="batch_processing must be a boolean"):
        config.snapshot()


@pytest.mark.anyio
async def test_configured_route_helpers_do_not_load_main_agent(monkeypatch):
    async def unexpected(*args, **kwargs):
        raise AssertionError("MainAgent loaded before Docling")

    monkeypatch.setattr("agent.routing.ask_main_agent", unexpected)
    state = State(config=configuration().snapshot())
    state.VLM_INPUT = (await match_vlm_models(state))["VLM_INPUT"]
    result = await match_formatter_models(state)
    assert set(result["FORMATTER_INPUT"]) == {"reader_a", "reader_b"}


@pytest.mark.anyio
async def test_all_routes_finish_before_serial_model_turns(monkeypatch):
    runtime, state = runtime_for_test(monkeypatch, active_manifests=["manifest"])
    state.vlm_queues = await create_model_queues(
        runtime.client, run_id=state.run_id, stage="vlm",
        model_keys={"reader_a", "reader_b"},
    )
    state.formatter_queues = await create_model_queues(
        runtime.client, run_id=state.run_id, stage="formatter", model_keys={"formatter"},
    )
    manifest = {"assets": [{"images_meta": [{"redis_key": "a"}, {"redis_key": "b"}]}]}
    await runtime.client.set("manifest", json.dumps(manifest))
    events = []

    @asynccontextmanager
    async def loaded(key):
        events.append("load:" + key)
        try:
            yield
        finally:
            events.append("unload:" + key)

    async def save_route(state, key, image_key, stage, field, queues):
        events.append("route:" + stage + ":" + image_key)
        saved = await runtime.manifest(key)
        image = next(i for i in saved["assets"][0]["images_meta"] if i["redis_key"] == image_key)
        image[field] = "reader_" + image_key if stage == "vlm" else "formatter"
        await runtime.client.set(key, json.dumps(saved))

    async def process_image(job):
        events.append("infer:" + job["model"] + ":" + job["image_key"])
        saved = await runtime.manifest(job["manifest_key"])
        image = next(i for i in saved["assets"][0]["images_meta"] if i["redis_key"] == job["image_key"])
        image["vlm_status" if job["stage"] == "vlm" else "format_status"] = "complete"
        await runtime.client.set(job["manifest_key"], json.dumps(saved))

    monkeypatch.setattr(runtime.batch_models, "loaded", loaded)
    monkeypatch.setattr(runtime, "save_route", save_route)
    monkeypatch.setattr(runtime, "process_image", process_image)
    try:
        await runtime.route_ready_images(state, stage="vlm", thinking=True)
        await runtime.enqueue_ready_images(state, "vlm")
        await runtime.route_ready_images(state, stage="formatter", thinking=False)
        await runtime.enqueue_ready_images(state, "formatter")
        assert events == [
            "load:router", "route:vlm:a", "route:vlm:b", "unload:router",
            "load:reader_a", "infer:reader_a:a", "unload:reader_a",
            "load:reader_b", "infer:reader_b:b", "unload:reader_b",
            "route:formatter:a", "route:formatter:b", "load:formatter",
            "infer:formatter:a", "infer:formatter:b", "unload:formatter",
        ]
        # Replaying a checkpoint must not reload models for already saved work.
        events.clear()
        await runtime.route_ready_images(state, stage="vlm", thinking=True)
        await runtime.enqueue_ready_images(state, "vlm")
        assert events == []
    finally:
        await runtime.close()


@pytest.mark.anyio
async def test_extraction_capacity_resume_and_final_partial_batch(monkeypatch, tmp_path):
    for name in ["a.pdf", "b.pdf", "c.pdf"]:
        (tmp_path / name).touch()
    runtime, _ = runtime_for_test(monkeypatch, input_path=str(tmp_path))
    batches = []

    async def no_models(*args):
        pass

    async def extract(paths):
        batches.append(paths)
        for path in paths:
            key = "manifest:" + path
            manifest = {"status": "completed", "assets": [{"images_meta": [{"redis_key": key + ":image"}]}]}
            await runtime.client.set(key, json.dumps(manifest))
            await runtime.client.rpush(runtime.key("manifests"), key)
            await runtime.client.set(runtime.key("source:" + hashlib.sha256(path.encode()).hexdigest()), key)

    monkeypatch.setattr(runtime.batch_models, "clear", no_models)
    monkeypatch.setattr(runtime.batch_models, "check_memory", no_models)
    monkeypatch.setattr(runtime, "extract_batch", extract)
    try:
        first = await runtime.active_manifest_keys()
        assert len(first) == 2
        assert await runtime.active_manifest_keys() == first
        assert len(batches) == 1
        assert not await runtime.client.exists(runtime.key("producer_done"))
        await runtime.client.sadd(runtime.key("stages_completed"), *first)
        assert len(await runtime.active_manifest_keys()) == 1
        assert list(map(len, batches)) == [2, 1]
        assert await runtime.client.exists(runtime.key("producer_done"))
    finally:
        await runtime.close()


@pytest.mark.anyio
async def test_unrelated_residency_is_not_unloaded(monkeypatch):
    monkeypatch.setenv("BATCH_TEST_URL", "http://localhost:9999/v1")
    lifecycle = BatchModelLifecycle(configuration().snapshot())
    calls = []

    async def request(base, path, payload=None):
        calls.append(path)
        return {"models": [{"name": "unrelated:latest"}]}

    monkeypatch.setattr(lifecycle, "request", request)
    with pytest.raises(RuntimeError, match="unrelated resident models"):
        await lifecycle.clear()
    assert calls == ["/api/ps"]


@pytest.mark.anyio
async def test_inference_failure_still_unloads_and_confirms_release(monkeypatch):
    monkeypatch.setenv("BATCH_TEST_URL", "http://localhost:9999/v1")
    lifecycle = BatchModelLifecycle(configuration().snapshot())
    resident = []
    calls = []

    async def request(base, path, payload=None):
        calls.append((path, payload))
        if path == "/api/generate":
            resident.clear()
            return {"done": True}
        return {"models": [{"name": name} for name in resident]}

    async def memory(key=None):
        pass

    monkeypatch.setattr(lifecycle, "request", request)
    monkeypatch.setattr(lifecycle, "check_memory", memory)
    with pytest.raises(ValueError, match="inference failed"):
        async with lifecycle.loaded("reader_a"):
            resident.append("reader_a:latest")
            raise ValueError("inference failed")
    assert not resident
    assert ("/api/generate", {"model": "reader_a:latest", "keep_alive": 0, "stream": False}) in calls
    assert calls[-1] == ("/api/ps", None)
