"""Exercise the real graph and workers with Redis emulation and SQLite SQL."""

import importlib
import json

import fakeredis
import fakeredis.aioredis
import pytest
from parser.src.db import runs, tables
from parser.src.docling.type_format import Asset, AssetContext, ImageMeta, Manifest
from parser.src.redis.queues import create_model_queues
from sqlalchemy import create_engine, func, select

from agent import hooks, inference, startup
from agent.resources import compatible_groups
from agent.runtime import Runtime
from agent.state import RedisState

from .test_configuration import configured
from .test_db_batches import count_rows as count
from .test_formatter import model_response

pytestmark = pytest.mark.anyio


async def exercise(
    tmp_path,
    monkeypatch,
    *,
    pdfs,
    fail=False,
    omit=False,
    retry=False,
    writer_failure=False,
):
    graph_module = importlib.import_module("agent.graph")
    runtime_module = importlib.import_module("agent.runtime")
    inputs = tmp_path / "pdfs"
    inputs.mkdir()
    for index in range(pdfs):
        (inputs / f"{index:03}.pdf").write_bytes(b"test")
    engine = create_engine(f"sqlite:///{tmp_path / 'run.sqlite'}")
    tables.metadata.create_all(engine)
    config = configured()
    for model in config.models.values():
        model.endpoint_env = "TEST_MODEL_URL"
    monkeypatch.setenv("TEST_MODEL_URL", "http://localhost:9999/v1")
    config.approve_with_fails = "omit" if omit else False
    config.max_image_retries = 1
    runs.start_run(engine, run_id="run", config=config)
    context = runs.load_run_context(engine, "run")
    server = fakeredis.FakeServer()
    sync = fakeredis.FakeRedis(server=server)
    client = fakeredis.aioredis.FakeRedis(server=server)
    monkeypatch.setattr(startup, "load_agent_context", lambda _: context)
    monkeypatch.setattr(startup, "inspect_hardware", lambda: {})

    async def ensure(state):
        return {"redis": RedisState(status="ready")}

    monkeypatch.setattr(hooks, "ensure_redis", ensure)

    async def queues(state, stage):
        keys = set(
            (state.VLM_INPUT if stage == "vlm" else state.FORMATTER_INPUT).values()
        )
        return {
            f"{stage}_queues": await create_model_queues(
                client, run_id=state.run_id, stage=stage, model_keys=keys
            )
        }

    async def vlm_queues(state):
        return await queues(state, "vlm")

    async def formatter_queues(state):
        return await queues(state, "formatter")

    monkeypatch.setattr(graph_module, "create_vlm_queues", vlm_queues)
    monkeypatch.setattr(graph_module, "create_formatter_queues", formatter_queues)

    def extract_pdf(worker, path, **kwargs):
        key = f"manifest:{path.stem}"
        images = [
            ImageMeta(
                redis_key=f"{key}:image:{i}",
                manifest_key=key,
                page_number=1,
                width=20,
                height=20,
                class_name="bar_chart",
            )
            for i in range(2)
        ]
        for i, image in enumerate(images):
            sync.hset(
                image.redis_key,
                "png",
                b"fail" if fail == "all" or (i == 0 and fail) else b"ok",
            )
        return Manifest(
            schema_version=2,
            document_id=path.stem,
            pdf_sha256=path.stem,
            source_path=str(path),
            name=path.stem,
            started_at="now",
            status="completed",
            manifest_key=key,
            run_id="run",
            schema_id=context["schema_id"],
            approve_with_fails=config.approve_with_fails,
            assets=[
                Asset(
                    asset_id=path.stem,
                    name="figure",
                    images_meta=images,
                    context=AssetContext(caption="Source caption"),
                )
            ],
        )

    monkeypatch.setattr(runtime_module.DocumentWorker, "process_pdf", extract_pdf)
    calls = {"vlm": 0, "formatter": 0}

    async def extract(model, png, context):
        calls["vlm"] += 1
        if png == b"fail":
            raise ValueError("unreadable image")
        return "original observations"

    async def format_result(model, raw, schema, context, previous_error=None):
        calls["formatter"] += 1
        if retry and calls["formatter"] == 1:
            raise ValueError("invalid JSON")
        return {
            **json.loads(model_response()),
            "model": model["model_id"],
            "schema": schema,
            "model_response": model_response(),
            "error": None,
        }

    monkeypatch.setattr(inference, "extract", extract)
    monkeypatch.setattr(inference, "format_result", format_result)
    runtime = None

    async def runtime_for(state):
        nonlocal runtime
        if runtime is None:
            runtime = Runtime(state, client=client, sync_client=sync, engine=engine)
            await runtime.start()
        return runtime

    monkeypatch.setattr(hooks, "runtime_for", runtime_for)
    batches = []
    original_write = runtime_module.write_batches

    def write(engine, manifests, **kwargs):
        if writer_failure:
            raise RuntimeError("Database is unavailable")
        batches.append(len(manifests))
        return original_write(engine, manifests, **kwargs)

    monkeypatch.setattr(runtime_module, "write_batches", write)
    import asyncio

    try:
        result = await asyncio.wait_for(
            graph_module.build_graph().ainvoke(
                {"input_path": str(inputs), "run_id": "run"}, {"recursion_limit": 10000}
            ),
            timeout=30,
        )
        return result, engine, calls, batches, sync
    finally:
        if runtime and not runtime.closed:
            await runtime.close()


@pytest.mark.parametrize("pdfs", [0, 3, 21, 51])
async def test_full_graph_buffers_and_flushes_tail(tmp_path, monkeypatch, pdfs):
    result, engine, calls, batches, _ = await exercise(tmp_path, monkeypatch, pdfs=pdfs)
    assert result["status"] == "completed"
    assert count(engine, tables.documents) == pdfs
    assert count(engine, tables.images) == pdfs * 2
    assert calls == {"vlm": pdfs * 2, "formatter": pdfs * 2}
    assert batches == ([50, 1] if pdfs == 51 else [pdfs] if pdfs else [])
    assert result["docling"].queued_pdfs == pdfs
    assert bool(result["docling"].filled_at) == (pdfs >= 20)
    with engine.connect() as connection:
        assert connection.scalar(select(tables.run_info.c.outcome)) == "completed"


@pytest.mark.parametrize("omit", [False, True])
async def test_terminal_image_failure_obeys_document_policy(
    tmp_path, monkeypatch, omit
):
    result, engine, calls, _, _ = await exercise(
        tmp_path, monkeypatch, pdfs=1, fail=True, omit=omit
    )
    assert result["status"] == "completed_with_errors"
    assert count(engine, tables.documents) == int(omit)
    assert calls == {"vlm": 3, "formatter": 1}
    if omit:
        with engine.connect() as connection:
            assert (
                connection.scalar(
                    select(func.count())
                    .select_from(tables.images)
                    .where(tables.images.c.failed)
                )
                == 1
            )


async def test_formatter_retry_does_not_repeat_vlm(tmp_path, monkeypatch):
    result, engine, calls, _, _ = await exercise(
        tmp_path, monkeypatch, pdfs=1, retry=True
    )
    assert result["status"] == "completed"
    assert calls == {"vlm": 2, "formatter": 3}
    assert count(engine, tables.documents) == 1


async def test_all_failed_omit_document_is_written_before_finalization(
    tmp_path, monkeypatch
):
    result, engine, calls, _, _ = await exercise(
        tmp_path, monkeypatch, pdfs=1, fail="all", omit=True
    )
    assert result["status"] == "completed_with_errors"
    assert calls == {"vlm": 4, "formatter": 0}
    assert count(engine, tables.documents) == 1


async def test_writer_failure_records_a_fatal_run_and_retains_work(
    tmp_path, monkeypatch
):
    result, engine, _, _, _ = await exercise(
        tmp_path, monkeypatch, pdfs=1, writer_failure=True
    )
    assert result["status"] == "failed"
    assert count(engine, tables.documents) == 0
    with engine.connect() as connection:
        row = connection.execute(
            select(tables.run_info.c.status, tables.run_info.c.outcome)
        ).one()
    assert row == ("incomplete", "failed")


def test_resource_groups_enforce_both_memory_budgets():
    config = configured().snapshot()
    for model in config["models"].values():
        model.update(
            serving="managed", device="cuda", estimated_vram_gib=6, estimated_ram_gib=4
        )
    hardware = {"ram_available_gib": 20, "gpus": [{"vram_available_gib": 10}]}
    assert len(compatible_groups(config, hardware, set(config["models"]))) == 2
    hardware["gpus"][0]["vram_available_gib"] = 16
    assert len(compatible_groups(config, hardware, set(config["models"]))) == 1
    hardware["ram_available_gib"] = 3
    with pytest.raises(ValueError, match="does not fit"):
        compatible_groups(config, hardware, set(config["models"]))
