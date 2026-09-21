"""Run checkpointed MainAgent startup decisions with bounded operational recovery."""

import asyncio
import json
import os
from dataclasses import replace
from datetime import datetime, timezone

from parser.src.db.runs import finish_run
from parser.src.tools.tools import ToolInputError, observation
from redis.exceptions import RedisError
from sqlalchemy import create_engine

from agent import main_agent
from agent.diagnostic_redaction import redact_diagnostic, redact_observation
from agent.model_lifecycle import BatchModelLifecycle
from agent.progress_messages import show_progress, startup_action
from agent.resources import compatible_groups
from agent.routing import match_formatter_models, match_vlm_models
from agent.startup_tools import available_startup_tools, execute_startup_tool


def stopped(state, reason):
    """Keep a bounded explanation of why startup could not continue."""
    show_progress(state.config, f"Startup stopped: {reason}")
    return {"status": "failed", "errors": [*state.errors, redact_diagnostic(reason)]}


async def select_startup_tool(state):
    """Ask the configured MainAgent what to do next, including when routing is fixed."""
    if state.agent_tool_call:
        return {}  # Resume the saved call instead of asking for a replacement.
    if state.startup_turns >= state.config.get("startup_max_turns", 24):
        return stopped(state, "Startup exhausted its decision budget. Last failure: " + str(state.startup_last_error))
    if state.agent_input_errors >= main_agent.MAX_INPUT_ERRORS:
        return stopped(state, "MainAgent repeatedly returned invalid startup tool arguments")
    try:
        if not state.config.get("main_agent"):
            return await select_configured_operation(state)
        specs = available_startup_tools(state)
        show_progress(state.config, "MainAgent: choosing the next startup action.")
        if state.config.get("batch_processing"):
            # No Redis runtime is needed for startup decisions or Redis recovery.
            # Unload before executing tools, especially the Docling handoff.
            async with BatchModelLifecycle(state.config).loaded(state.config["main_agent"]):
                update = await main_agent.decide(state, tool_specs=specs)
        else:
            update = await main_agent.decide(state, tool_specs=specs)
    except Exception as exc:
        return stopped(state, f"Startup could not select the next tool: {type(exc).__name__}: {exc}")
    return {**update, "startup_turns": state.startup_turns + 1}


async def select_configured_operation(state):
    """Preserve explicit main_agent=None without silently bypassing a configured model."""
    if state.startup_last_error:
        return stopped(state, "Startup failed without a configured MainAgent: " + str(state.startup_last_error))
    spec = available_startup_tools(state)[0]["function"]
    name = spec["name"]
    arguments = {}
    if name == "set_vlm_routes":
        arguments["routes"] = (await match_vlm_models(state))["VLM_INPUT"]
    elif name == "set_formatter_routes":
        arguments["routes"] = (await match_formatter_models(state))["FORMATTER_INPUT"]
    elif name == "schedule_models":
        arguments["groups"] = compatible_groups(
            state.config, state.current_hardware, set(state.vlm_queues) | set(state.formatter_queues),
        )
    call = {"id": f"configured-startup-{state.startup_turns}", "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments)}}
    return {"agent_tool_call": call, "startup_turns": state.startup_turns + 1,
            "agent_messages": [*state.agent_messages, {"role": "assistant", "content": None, "tool_calls": [call]}]}


def selected_startup_node(state):
    """Dispatch the checkpointed tool call to its visible graph node."""
    if state.status == "failed":
        return "startup_failed"
    if not state.agent_tool_call:
        return "main_agent_startup"
    name = state.agent_tool_call["function"]["name"]
    from agent.startup_tools import STARTUP_TOOL_NAMES

    return name if name in STARTUP_TOOL_NAMES else "invalid_startup_tool"


async def execute_selected_tool(state):
    """Return operational errors to MainAgent instead of terminating the graph."""
    call = state.agent_tool_call
    if call is None:
        raise ValueError("Startup execution requires a checkpointed tool call")
    name = call["function"]["name"]
    actor = "MainAgent" if state.config.get("main_agent") else "Configured startup"
    show_progress(state.config, f"{actor}: selected to {startup_action(name)}.")
    update, error = {}, None
    input_error = False
    try:
        try:
            arguments = json.loads(call["function"]["arguments"],
                                   parse_constant=main_agent._reject_constant,
                                   object_pairs_hook=main_agent._unique_object)
        except (ValueError, TypeError) as exc:
            raise ToolInputError(f"Tool arguments must be valid JSON: {exc}") from exc
        update = await execute_startup_tool(state, name, arguments)
    except Exception as exc:
        input_error = isinstance(exc, ToolInputError)
        error = {"tool": name, "kind": "input" if input_error else "operational",
                 "type": type(exc).__name__, "message": redact_diagnostic(exc)}
        if isinstance(exc, RedisError) or name == "ensure_redis":
            update["redis"] = replace(state.redis, status="failed")
    attempts = {**state.startup_attempts, name: state.startup_attempts.get(name, 0) + 1}
    if error:
        show_progress(state.config, f"Startup tool failed: {error['message']}. Returning the result for the next decision.")
    elif update.get("status") == "failed":
        show_progress(state.config, "Startup stopped: " + "; ".join(update.get("errors", [])))
    else:
        show_progress(state.config, f"Startup finished: {startup_action(name)}.")
    last_error = error
    if error is None and name in {"diagnose_redis", "inspect_startup_resources"}:
        last_error = state.startup_last_error
    update.update(startup_attempts=attempts, startup_last_error=last_error)
    if "errors" in update:
        update["errors"] = redact_observation(update["errors"])
    result = {"ok": False, "error": error} if error else {
        "ok": True, "state": observation(replace(state, **update)),
    }
    messages = [*state.agent_messages, {"role": "tool", "tool_call_id": call["id"],
                "content": json.dumps(redact_observation(result), allow_nan=False)}]
    # Trim only at complete assistant/tool pair boundaries.
    messages = messages[-12:]
    while messages and messages[0]["role"] != "assistant":
        messages.pop(0)
    return {**update, "agent_tool_call": None, "agent_messages": messages,
            "agent_input_errors": state.agent_input_errors + 1 if input_error else 0}


def after_startup_tool(state):
    """Return tool results to the agent, or hand off only after startup succeeds."""
    if state.status == "failed":
        return "startup_failed"
    if state.docling.status != "idle" and not state.startup_last_error:
        return "docling"
    return "main_agent_startup"


async def record_startup_failure(state):
    """Record failure without constructing a runtime that depends on working Redis."""
    def record():
        engine = create_engine(os.environ["DATABASE_URL"])
        try:
            finish_run(engine, state.run_id, outcome="failed", errors=state.errors)
        finally:
            engine.dispose()

    await asyncio.to_thread(record)
    return {"status": "failed", "completed_at": datetime.now(timezone.utc).isoformat()}
