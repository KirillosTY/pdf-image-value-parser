"""Verify MainAgent decisions, guarded tools, transport and checkpoint replay."""

import importlib
import json
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from openai.types.chat import ChatCompletion
from parser.src.tools import tools

from agent import hooks, main_agent, model
from agent.state import RedisState, State, WorkerEvent

from .test_configuration import configured

pytestmark = pytest.mark.anyio


def agent_state():
    config = configured()
    config.models["controller"] = deepcopy(config.models["format"])
    config.models["controller"].roles = ["main_agent"]
    config.models["controller"].endpoint_env = "TEST_AGENT_URL"
    config.main_agent = "controller"
    return State(run_id="run", input_path="/pdfs", config=config.snapshot(),
                 system_prompt="Approved configuration and schema", fill_threshold=1)


async def choose_available_tool(state, specs, context):
    """Simulate a model using the supplied native tool schemas."""
    spec = specs[0]["function"]
    properties = spec["parameters"]["properties"]
    arguments = {}
    if "routes" in properties:
        arguments["routes"] = {
            key: value["enum"][0]
            for key, value in properties["routes"]["properties"].items()
        }
    if "groups" in properties:
        # Deliberately select serial groups instead of the greedy default.
        arguments["groups"] = [[key] for key in properties["groups"]["items"]["items"]["enum"]]
    return {"role": "assistant", "content": None, "tool_calls": [{
        "id": f"call-{spec['name']}", "type": "function",
        "function": {"name": spec["name"], "arguments": json.dumps(arguments)},
    }]}


async def test_tools_reject_unavailable_actions_and_unapproved_routes():
    state = agent_state()
    with pytest.raises(tools.ToolInputError, match="not available"):
        await tools.execute_tool(state, "finish_run", {})
    routes = {key: "vision" for key in [*state.config["docling_charts"], "unknown"]}
    routes["bar_chart"] = "controller"
    with pytest.raises(tools.ToolInputError):
        await tools.execute_tool(state, "set_vlm_routes", {"routes": routes})
    assert state.VLM_INPUT == {}


async def test_thinking_routes_are_model_decisions():
    state = agent_state()
    state.config["models"]["other"] = deepcopy(state.config["models"]["vision"])
    state.config["vlm_think_sorting"] = True
    routes = {key: "other" for key in [*state.config["docling_charts"], "unknown"]}
    routes["unknown"] = "vision"
    assert await tools.execute_tool(state, "set_vlm_routes", {"routes": routes}) == {"VLM_INPUT": routes}


async def test_invalid_json_and_repeated_decisions_are_bounded(monkeypatch):
    state = agent_state()

    async def invalid(state, specs, context):
        message = await choose_available_tool(state, specs, context)
        message["tool_calls"][0]["function"]["arguments"] = '{"routes": {}, "routes": {}}'
        return message

    monkeypatch.setattr(main_agent, "ask_main_agent_tool", invalid)
    for _ in range(3):
        state = replace(state, **await main_agent.decide(state))
        state = replace(state, **await main_agent.act(state))
        assert not json.loads(state.agent_messages[-1]["content"])["ok"]
        assert not state.VLM_INPUT
    with pytest.raises(RuntimeError, match="3 attempts"):
        await main_agent.decide(state)


async def test_native_tool_transport_sends_prompt_and_rejects_truncation(monkeypatch):
    state = agent_state()
    monkeypatch.setenv("TEST_AGENT_URL", "http://localhost:9999/v1")
    calls = []
    finish_reason = "tool_calls"

    class Client:
        def __init__(self, **kwargs):
            self.chat = SimpleNamespace(completions=self)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def create(self, **kwargs):
            calls.append(kwargs)
            return ChatCompletion(
                id="response", created=0, model="test", object="chat.completion",
                choices=[{"index": 0, "finish_reason": finish_reason, "message": {
                    "role": "assistant", "content": None, "tool_calls": [{
                        "id": "call", "type": "function", "function": {
                            "name": "set_vlm_routes", "arguments": "{}",
                        },
                    }],
                }}],
            )

    monkeypatch.setattr(model, "AsyncOpenAI", Client)
    result = await model.ask_main_agent_tool(state, tools.available_tools(state), "progress")
    assert result["tool_calls"][0]["function"]["name"] == "set_vlm_routes"
    assert calls[0]["messages"][0]["content"] == state.system_prompt
    assert calls[0]["tool_choice"] == "required"
    assert calls[0]["parallel_tool_calls"] is False
    finish_reason = "length"
    with pytest.raises(ValueError, match="complete tool call"):
        await model.ask_main_agent_tool(state, tools.available_tools(state), "progress")


async def test_agent_checkpoint_resumes_saved_tool_and_waits_for_real_events(monkeypatch):
    graph = importlib.import_module("agent.graph")
    initial = agent_state()

    async def prepare(state):
        return {"config": initial.config, "system_prompt": initial.system_prompt}

    monkeypatch.setattr(graph, "prepare_agent", prepare)
    decisions = []

    async def choose(state, specs, context):
        message = await choose_available_tool(state, specs, context)
        decisions.append(message["tool_calls"][0]["function"]["name"])
        return message

    monkeypatch.setattr(main_agent, "ask_main_agent_tool", choose)
    effects = []
    attempts = 0

    async def ensure(state):
        return {"redis": RedisState(status="ready")}

    async def vlm_queues(state):
        return {"vlm_queues": {"vision": "vision-queue"}}

    async def formatter_queues(state):
        return {"formatter_queues": {"format": "format-queue"}}

    async def effect(state, event=None):
        effects.append(event.kind if event else "lifecycle")
        return {}

    async def start(state):
        nonlocal attempts
        attempts += 1
        assert state.resource_plan == [["format"], ["vision"]]
        if attempts == 1:
            raise RuntimeError("temporary service failure")
        return {}

    monkeypatch.setattr(hooks, "ensure_redis", ensure)
    monkeypatch.setattr(tools.queues, "create_vlm_queues", vlm_queues)
    monkeypatch.setattr(tools.queues, "create_formatter_queues", formatter_queues)
    monkeypatch.setattr(hooks, "start_docling", start)
    for name in ("start_vlm_pool", "start_formatter_pool", "analyze_vlm_result",
                 "build_db_fields", "write_chart_record", "acknowledge_pdf",
                 "acknowledge_worker_event", "finish_run"):
        monkeypatch.setattr(hooks, name, effect)
    events = iter([
        ("docling.pdf_queued", {"image_keys": ["image"]}),
        ("docling.completed", {}),
        ("vlm.pdf_claimed", {}),
        ("vlm.image_completed", {"payload_ref": "raw"}),
        ("analysis.completed", {"payload_ref": "formatted", "chart_type": "bar_chart", "unmapped_observations_ref": "unmapped"}),
        ("db_fields.completed", {"payload_ref": "fields"}),
        ("writer.committed", {"record_id": "stored-record"}),
    ])

    async def receive(state):
        kind, payload = next(events)
        return WorkerEvent(event_id=kind, run_id="run", kind=kind, source="test",
                           timestamp="now", document_attempt_id="pdf", image_key="image", **payload)

    monkeypatch.setattr(hooks, "wait_for_worker_event", receive)
    compiled = graph.build_graph(checkpointer=InMemorySaver())
    invocation = {"configurable": {"thread_id": "main-agent"}, "recursion_limit": 200}
    with pytest.raises(RuntimeError, match="temporary service failure"):
        await compiled.ainvoke(initial, invocation)
    snapshot = await compiled.aget_state(invocation)
    assert snapshot.values["agent_tool_call"]["function"]["name"] == "start_docling"
    assert snapshot.next == ("main_agent_tool",)
    result = await compiled.ainvoke(None, invocation)
    assert result["status"] == "completed"
    assert result["images"]["image"].record_id == "stored-record"
    assert result["agent_tool_call"] is None
    assert decisions.count("start_docling") == 1
    assert attempts == 2
    assert decisions.count("wait_for_worker_event") == 7
    assert decisions[-1] == "finish_run"
    assert len(result["agent_messages"]) <= 12
    assert result["agent_messages"][0]["role"] == "assistant"
    assert result["agent_messages"][-1]["role"] == "tool"
