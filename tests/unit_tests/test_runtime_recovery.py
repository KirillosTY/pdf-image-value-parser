"""Verify durable event replay, routing boundaries, and resource placement."""

import json
from copy import deepcopy
from types import SimpleNamespace

import fakeredis
import fakeredis.aioredis
import pytest
from parser.src.docling.docling_tool import top_classification
from parser.src.redis.queues import WORKER_GROUP, create_model_queues

from agent import routing
from agent.resources import compatible_groups
from agent.runtime import Runtime
from agent.state import State

from .test_configuration import configured

pytestmark = pytest.mark.anyio


def runtime_for_test():
    server = fakeredis.FakeServer()
    client = fakeredis.aioredis.FakeRedis(server=server)
    sync = fakeredis.FakeRedis(server=server)
    state = State(run_id="run", config=configured().snapshot())
    runtime = Runtime(
        state,
        client=client,
        sync_client=sync,
        engine=SimpleNamespace(dispose=lambda: None),
    )
    return runtime


async def test_events_survive_acknowledgment_replay_without_claiming_next_event():
    runtime = runtime_for_test()
    try:
        await runtime.client.xgroup_create(
            runtime.events, "coordinator", id="0-0", mkstream=True
        )
        await runtime.emit("docling.completed")
        await runtime.emit("docling.completed")
        event = await runtime.next_event()
        assert await runtime.client.xlen(runtime.events) == 1
        await runtime.acknowledge_event(event)
        await runtime.emit("worker.failed", source="writer", error="test")
        await runtime.acknowledge_event(event)
        assert (await runtime.next_event()).kind == "worker.failed"
    finally:
        await runtime.close()


async def test_pending_model_job_precedes_new_work_and_queues_are_run_scoped():
    runtime = runtime_for_test()
    try:
        queues = await create_model_queues(
            runtime.client, run_id="run", stage="vlm", model_keys={"vision"}
        )
        other = await create_model_queues(
            runtime.client, run_id="other", stage="vlm", model_keys={"vision"}
        )
        queue = queues["vision"]
        await runtime.enqueue_once(queue, "first", {"image": 1})
        first = await runtime.claim(queue, WORKER_GROUP, "worker-0")
        await runtime.enqueue_once(queue, "second", {"image": 2})
        assert (await runtime.claim(queue, WORKER_GROUP, "worker-0"))[1] == {"image": 1}
        await runtime.client.xack(queue, WORKER_GROUP, first[0])
        assert await runtime.queue_has_work(queue)
        second = await runtime.claim(queue, WORKER_GROUP, "worker-0")
        assert second[1] == {"image": 2}
        await runtime.client.xack(queue, WORKER_GROUP, second[0])
        assert not await runtime.queue_has_work(queue)
        assert await runtime.client.xlen(other["vision"]) == 0
    finally:
        await runtime.close()


async def test_thinking_routes_use_context_and_only_prepared_models(monkeypatch):
    state = State(
        config={"vlm_think_sorting": True, "formatter_think_sorting": True},
        vlm_queues={"a": "qa", "b": "qb"},
        formatter_queues={"x": "qx", "y": "qy"},
    )
    prompts = []

    async def ask(state, prompt):
        prompts.append(prompt)
        return json.dumps({"model": "b" if len(prompts) == 1 else "y"})

    monkeypatch.setattr(routing, "ask_main_agent", ask)
    image = {
        "classifications": [
            {"class_name": str(i), "confidence": 1 - i / 10} for i in range(3)
        ]
    }
    context = {
        "caption": "Caption",
        "mentions": [{"text": "Mention"}],
        "nearby": [{"text": "Nearby"}],
    }
    assert await routing.route_image(state, image, context) == "b"
    assert await routing.route_result(state, "b", "raw observations", context) == "y"
    assert all(word in prompts[0] for word in ("Caption", "Mention", "Nearby", "0.8"))
    assert "raw observations" in prompts[1]

    async def invalid(state, prompt):
        return '{"model": "unconfigured"}'

    monkeypatch.setattr(routing, "ask_main_agent", invalid)
    with pytest.raises(ValueError, match="unavailable"):
        await routing.route_image(state, image, context)


def test_managed_groups_respect_the_selected_gpu():
    config = configured().snapshot()
    for model in config["models"].values():
        model.update(
            serving="managed", device="cuda", estimated_vram_gib=6, estimated_ram_gib=4
        )
    hardware = {
        "ram_available_gib": 20,
        "gpus": [{"vram_available_gib": 10}, {"vram_available_gib": 10}],
    }
    assert len(compatible_groups(config, hardware, set(config["models"]))) == 2
    config["models"]["format"]["gpu_index"] = 1
    assert len(compatible_groups(config, hardware, set(config["models"]))) == 1
    config["models"]["format"]["estimated_ram_gib"] = None
    with pytest.raises(ValueError, match="estimates"):
        compatible_groups(config, hardware, set(config["models"]))


def test_docling_preserves_top_three_predictions():
    predictions = [
        dict(class_name=str(i), confidence=score)
        for i, score in enumerate([0.1, 0.9, 0.5, 0.7])
    ]
    picture = SimpleNamespace(
        meta=SimpleNamespace(
            classification=SimpleNamespace(
                predictions=[
                    SimpleNamespace(
                        model_dump=lambda mode, value=value: deepcopy(value)
                    )
                    for value in predictions
                ]
            )
        )
    )
    best = top_classification(picture)
    assert best["confidence"] == 0.9
    assert [entry["confidence"] for entry in best["predictions"]] == [0.9, 0.7, 0.5]
