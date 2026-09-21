"""Exercise tool-driven startup and Redis recovery with all external effects mocked."""

import importlib
import json
from contextlib import asynccontextmanager
from dataclasses import replace

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from redis.exceptions import ConnectionError

from agent import (
    hooks,
    main_agent,
    redis_recovery,
    startup_orchestration,
    startup_tools,
)
from agent.diagnostic_redaction import redact_diagnostic
from agent.state import RedisState, State
from config import RedisRecoveryConfig

from .test_batch_processing import configuration


def message(name, arguments=None):
    call = {"id": name, "type": "function", "function": {
        "name": name, "arguments": json.dumps(arguments or {}),
    }}
    return {"role": "assistant", "content": None, "tool_calls": [call]}


@pytest.mark.anyio
async def test_graph_agent_selects_startup_tools_and_recovers_redis(monkeypatch):
    graph_module = importlib.import_module("agent.graph")
    config = configuration().snapshot()
    monkeypatch.setenv("BATCH_TEST_URL", "http://localhost:9999/v1")
    effects, decisions = [], []
    plan = iter([
        "ensure_redis", "diagnose_redis", "start_redis_service", "ensure_redis",
        "set_vlm_routes", "set_formatter_routes", "create_formatter_queues",
        "create_vlm_queues", "schedule_models", "start_docling",
    ])

    async def prepare(state):
        return {"config": config, "system_prompt": "Approved startup", "current_hardware": {}}

    @asynccontextmanager
    async def loaded(self, key):
        effects.append("load_main_agent")
        try:
            yield
        finally:
            effects.append("unload_main_agent")

    async def choose(state, specs, context):
        name = next(plan)
        decisions.append(name)
        spec = next(item["function"] for item in specs if item["function"]["name"] == name)
        arguments = {}
        if "routes" in spec["parameters"]["properties"]:
            arguments["routes"] = {
                key: value["enum"][0]
                for key, value in spec["parameters"]["properties"]["routes"]["properties"].items()
            }
        if name == "schedule_models":
            arguments["groups"] = [[key] for key in sorted(set(state.vlm_queues) | set(state.formatter_queues))]
        if name == "diagnose_redis":
            result = json.loads(state.agent_messages[-1]["content"])
            assert result["error"]["kind"] == "operational"
            assert "connection refused" in result["error"]["message"]
        return message(name, arguments)

    async def ensure(state):
        effects.append("ensure_redis")
        if effects.count("ensure_redis") == 1:
            raise ConnectionError("connection refused")
        return {"redis": RedisState(status="ready")}

    async def diagnose(policy):
        effects.append("diagnose_redis")
        return {"reachable": False, "kind": "connection", "repair_available": True}

    async def repair(policy):
        effects.append("start_redis_service")
        return {"reachable": True, "kind": "ready"}

    async def create_vlm(state):
        return {"vlm_queues": {"reader_a": "qa", "reader_b": "qb"}}

    async def create_formatter(state):
        return {"formatter_queues": {"formatter": "qf"}}

    async def start(state):
        assert effects[-1] == "unload_main_agent"
        effects.append("start_docling")
        return {}

    monkeypatch.setattr(graph_module, "prepare_agent", prepare)
    monkeypatch.setattr(startup_orchestration.BatchModelLifecycle, "loaded", loaded)
    monkeypatch.setattr(main_agent, "ask_main_agent_tool", choose)
    monkeypatch.setattr(hooks, "ensure_redis", ensure)
    monkeypatch.setattr(hooks, "start_docling", start)
    monkeypatch.setattr(startup_tools, "diagnose_redis", diagnose)
    monkeypatch.setattr(startup_tools, "start_redis_service", repair)
    monkeypatch.setattr(startup_tools.queues, "create_vlm_queues", create_vlm)
    monkeypatch.setattr(startup_tools.queues, "create_formatter_queues", create_formatter)
    monkeypatch.setattr(startup_tools, "inspect_hardware", lambda: {})
    graph = graph_module.build_graph(checkpointer=InMemorySaver())
    invocation = {"configurable": {"thread_id": "startup"}}
    # Save a selected tool, then resume it without another model decision.
    await graph.ainvoke({"run_id": "test", "input_path": "/pdfs"}, invocation,
                       interrupt_before=["ensure_redis"])
    assert decisions == ["ensure_redis"]
    result = await graph.ainvoke(None, invocation, interrupt_before=["docling"])
    assert result["status"] == "running"
    assert decisions.count("ensure_redis") == 2  # First failed, then the agent retried.
    assert decisions.index("create_formatter_queues") < decisions.index("create_vlm_queues")
    assert result["startup_attempts"]["ensure_redis"] == 2
    assert result["startup_last_error"] is None
    assert effects.count("start_docling") == 1
    assert effects.count("load_main_agent") == effects.count("unload_main_agent")


@pytest.mark.anyio
async def test_think_sorting_false_does_not_disable_startup_agent(monkeypatch):
    config = configuration()
    config.batch_processing = False
    config.think_sorting = False
    config.fallback_vlm = "reader_a"
    config.fallback_formatter = "formatter"
    config.chart_matcher = {key: "reader_a" for key in config.docling_charts}
    called = []

    async def choose(state, specs, context):
        called.append(True)
        return message("ensure_redis")

    monkeypatch.setattr(main_agent, "ask_main_agent_tool", choose)
    state = State(config=config.snapshot(), system_prompt="Approved setup")
    result = await startup_orchestration.select_startup_tool(state)
    assert called == [True]
    assert result["agent_tool_call"]["function"]["name"] == "ensure_redis"


@pytest.mark.anyio
async def test_startup_budget_and_saved_call_do_not_request_more_decisions(monkeypatch):
    config = configuration().snapshot()

    async def unexpected(*args, **kwargs):
        raise AssertionError("No additional model decision should be made")

    monkeypatch.setattr(main_agent, "decide", unexpected)
    state = State(config=config, startup_turns=config["startup_max_turns"])
    assert (await startup_orchestration.select_startup_tool(state))["status"] == "failed"
    state = replace(state, agent_tool_call=message("ensure_redis")["tool_calls"][0])
    assert await startup_orchestration.select_startup_tool(state) == {}


@pytest.mark.anyio
async def test_batch_resource_tool_rejects_concurrent_groups(monkeypatch):
    state = State(config=configuration().snapshot(), redis=RedisState(status="ready"),
                  VLM_INPUT={"unknown": "reader_a"}, FORMATTER_INPUT={"reader_a": "formatter"},
                  vlm_queues={"reader_a": "qa"}, formatter_queues={"formatter": "qf"})
    with pytest.raises(ValueError):
        await startup_tools.execute_startup_tool(state, "schedule_models", {"groups": [["reader_a", "formatter"]]})


@pytest.mark.anyio
async def test_redis_repair_starts_only_existing_service_and_verifies_ping(monkeypatch):
    policy = {**vars(RedisRecoveryConfig()), "enabled": True}
    monkeypatch.setenv("REDIS_URL", "redis://localhost:16379/0")
    probes = iter([{"reachable": False, "kind": "connection"}, {"reachable": True, "kind": "ready"}])
    commands = []

    async def probe():
        return next(probes)

    async def status(policy):
        return [{"State": "exited"}]

    async def compose(policy, arguments):
        commands.append(arguments)
        return ""

    monkeypatch.setattr(redis_recovery, "probe_connection", probe)
    monkeypatch.setattr(redis_recovery, "service_status", status)
    monkeypatch.setattr(redis_recovery, "run_compose", compose)
    result = await redis_recovery.start_redis_service(policy)
    assert result["reachable"] is True
    assert commands == [["start", "--wait", "--wait-timeout", "30"]]
    monkeypatch.setenv("REDIS_URL", "redis://remote.example:16379/0")
    with pytest.raises(ValueError, match="does not match"):
        await redis_recovery.start_redis_service(policy)
    assert len(commands) == 1


@pytest.mark.anyio
async def test_redis_repair_does_not_restart_running_service(monkeypatch):
    policy = {**vars(RedisRecoveryConfig()), "enabled": True}
    monkeypatch.setenv("REDIS_URL", "redis://localhost:16379/0")

    async def probe():
        return {"reachable": False, "kind": "connection"}

    async def status(policy):
        return [{"State": "running"}]

    async def unexpected(*args):
        raise AssertionError("Must not restart a running service")

    monkeypatch.setattr(redis_recovery, "probe_connection", probe)
    monkeypatch.setattr(redis_recovery, "service_status", status)
    monkeypatch.setattr(redis_recovery, "run_compose", unexpected)
    with pytest.raises(RuntimeError, match="not an existing stopped service"):
        await redis_recovery.start_redis_service(policy)


def test_startup_diagnostics_redact_credentials(monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://user:example-password@localhost:16379/0")
    monkeypatch.setenv("TEST_API_KEY", "example-token")
    text = redact_diagnostic("Failed redis://user:example-password@localhost:16379/0 token=example-token")
    assert "example-password" not in text
    assert "example-token" not in text


def test_recovery_scope_is_saved_and_disabled_for_old_snapshots():
    config = configuration()
    config.redis_recovery.enabled = True
    snapshot = config.snapshot()
    assert snapshot["redis_recovery"]["enabled"] is True
    del snapshot["redis_recovery"]
    from config import PipelineConfig

    assert PipelineConfig.from_dict(snapshot).redis_recovery.enabled is False
