import pytest

from agent import graph

pytestmark = pytest.mark.anyio


@pytest.mark.langsmith
async def test_agent_reports_unwired_redis_hook() -> None:
    inputs = {"input_path": "/example/pdfs"}
    with pytest.raises(NotImplementedError, match="ensure_redis"):
        await graph.ainvoke(inputs)


async def test_coordinator_resumes_failed_action_without_reextracting(monkeypatch):
    from langgraph.checkpoint.memory import InMemorySaver

    from agent import hooks
    from agent.graph import build_graph
    from agent.state import WorkerEvent

    notifications = iter([
        ("docling.pdf_queued", {"document_attempt_id": "pdf", "image_keys": ["image"]}),
        ("docling.completed", {}),
        ("vlm.pdf_claimed", {"document_attempt_id": "pdf"}),
        ("vlm.image_completed", {"payload_ref": "raw"}),
        ("analysis.completed", {"payload_ref": "normalized", "chart_type": "LINE_CHART",
                                "unmapped_observations_ref": "unmapped"}),
        ("db_fields.completed", {"payload_ref": "fields"}),
        ("writer.committed", {"record_id": "record"}),
    ])
    calls = []
    attempts = 0

    async def receive(state):
        kind, payload = next(notifications)
        if kind in {"vlm.image_completed", "analysis.completed", "db_fields.completed", "writer.committed"}:
            payload.update(document_attempt_id="pdf", image_key="image")
        return WorkerEvent(event_id=kind, run_id=state.run_id, kind=kind,
                           source=kind.split(".")[0], timestamp="now", **payload)

    def adapter(name):
        async def invoke(state, event=None):
            calls.append(name)
            return {}
        return invoke

    async def write(state, event):
        nonlocal attempts
        attempts += 1
        # Simulate an adapter/network failure before enqueueing the write.
        if attempts == 1:
            raise RuntimeError("writer temporarily unavailable")
        calls.append("write_chart_record")
        return {}

    for name in ("ensure_redis", "start_docling", "start_vlm_pool", "start_formatter_pool",
                 "analyze_vlm_result", "build_db_fields", "acknowledge_pdf",
                 "acknowledge_worker_event", "finish_run"):
        monkeypatch.setattr(hooks, name, adapter(name))
    monkeypatch.setattr(hooks, "wait_for_worker_event", receive)
    monkeypatch.setattr(hooks, "write_chart_record", write)
    compiled = build_graph(checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "resume-test"}, "recursion_limit": 100}
    with pytest.raises(RuntimeError, match="temporarily unavailable"):
        await compiled.ainvoke({"input_path": "/pdfs", "vlm": {"vlm_instances": 2}}, config)
    snapshot = await compiled.aget_state(config)
    assert snapshot.values["pending_actions"] == ["write_chart_record"]
    result = await compiled.ainvoke(None, config)
    assert result["status"] == "completed"
    assert result["images"]["image"].result_ref == "raw"
    assert result["images"]["image"].record_id == "record"
    assert result["vlm"].vlm_instances == 2
    assert attempts == 2
    assert calls.count("analyze_vlm_result") == 1
    assert calls.count("start_vlm_pool") == 1
    assert calls.count("start_formatter_pool") == 1
    assert calls.index("write_chart_record") < calls.index("acknowledge_pdf")
    assert calls[-1] == "finish_run"
