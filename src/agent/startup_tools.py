"""Offer startup operations and diagnostics according to their real prerequisites."""

import asyncio

from jsonschema import Draft202012Validator
from parser.src.tools.tools import ToolInputError, _routes, _tool

from agent import queues
from agent.redis_recovery import diagnose_redis, start_redis_service
from agent.resources import validate_resource_plan
from hardware import inspect_hardware

STARTUP_TOOL_NAMES = (
    "set_vlm_routes", "set_formatter_routes", "ensure_redis",
    "create_vlm_queues", "create_formatter_queues", "schedule_models",
    "start_docling", "diagnose_redis", "start_redis_service",
    "inspect_startup_resources", "abort_startup",
)


def available_startup_tools(state):
    """Let MainAgent choose among currently valid startup and recovery actions."""
    config = state.config
    vlms = {key for key, model in config["models"].items() if "vlm" in model["roles"]}
    formatters = {key for key, model in config["models"].items() if "formatter" in model["roles"]}
    tools = []
    if not state.VLM_INPUT:
        charts = list(dict.fromkeys([*config["docling_charts"], "unknown"]))
        fixed = None
        if not config["think_sorting"] or len(vlms) == 1:
            fixed = {chart: config["chart_matcher"].get(chart, config["fallback_vlm"])
                     for chart in charts}
        schema = _routes(charts, vlms, fixed)
        if config["fallback_vlm"]:
            schema["properties"]["unknown"]["enum"] = [config["fallback_vlm"]]
        tools.append(_tool("set_vlm_routes", "Set default chart-to-VLM routes; respect fixed choices.", {"routes": schema}))
    elif not state.FORMATTER_INPUT:
        selected = vlms if config["think_sorting"] else set(state.VLM_INPUT.values())
        fixed = None
        if not config["think_sorting"] or len(formatters) == 1:
            fixed = {key: config["format_matcher"].get(key, config["fallback_formatter"])
                     for key in selected}
        tools.append(_tool("set_formatter_routes", "Set default formatter routes for each selected VLM.", {
            "routes": _routes(sorted(selected), formatters, fixed),
        }))
    if state.redis.status != "ready":
        tools.append(_tool("ensure_redis", "Verify Redis connectivity; a failed check returns evidence for diagnosis."))
    else:
        if state.VLM_INPUT and not state.vlm_queues:
            tools.append(_tool("create_vlm_queues", "Create the selected VLM queues in Redis."))
        if state.FORMATTER_INPUT and not state.formatter_queues:
            tools.append(_tool("create_formatter_queues", "Create the selected formatter queues in Redis."))
        if state.vlm_queues and state.formatter_queues:
            if not state.resource_plan:
                tools.append(_tool("schedule_models", (
                    "Choose model groups and their order. Include every queued model exactly once. "
                    "In batch_processing mode every group must contain exactly one model."
                ), {"groups": {"type": "array", "minItems": 1, "items": {
                    "type": "array", "minItems": 1, "uniqueItems": True,
                    **({"maxItems": 1} if config.get("batch_processing") else {}),
                    "items": {"type": "string", "enum": sorted(set(state.vlm_queues) | set(state.formatter_queues))},
                }}}))
            elif state.docling.status == "idle":
                tools.append(_tool("start_docling", "Hand off to the configured pipeline after startup succeeds. This ends startup."))
    tools.append(_tool("diagnose_redis", "Inspect Redis connectivity, configured service status and a bounded log tail. Works without Redis queues."))
    diagnosis = state.startup_diagnostics.get("redis", {})
    if state.redis.status != "ready" and diagnosis.get("repair_available"):
        tools.append(_tool("start_redis_service", "Start only the configured existing stopped Redis service, then verify connectivity. Does not reset data."))
    tools.append(_tool("inspect_startup_resources", "Refresh measured RAM/VRAM and invalidate any stale resource plan."))
    tools.append(_tool("abort_startup", "Stop with a concrete explanation when startup cannot be recovered using the offered tools.", {
        "reason": {"type": "string", "minLength": 1, "maxLength": 1500},
    }))
    limit = config.get("startup_max_tool_attempts", 3)
    return [tool for tool in tools if tool["function"]["name"] == "abort_startup"
            or state.startup_attempts.get(tool["function"]["name"], 0) < limit]


async def execute_startup_tool(state, name, arguments):
    """Validate each call, perform its operation and return checkpointable results."""
    spec = next((item["function"] for item in available_startup_tools(state)
                 if item["function"]["name"] == name), None)
    if spec is None:
        raise ToolInputError(f"Startup tool {name!r} is unavailable or has exhausted its attempts")
    errors = list(Draft202012Validator(spec["parameters"]).iter_errors(arguments))
    if errors:
        raise ToolInputError(errors[0].message)
    if name == "set_vlm_routes":
        return {"VLM_INPUT": arguments["routes"]}
    if name == "set_formatter_routes":
        return {"FORMATTER_INPUT": arguments["routes"]}
    if name == "schedule_models":
        hardware = await asyncio.to_thread(inspect_hardware)
        try:
            validate_resource_plan(state.config, hardware,
                                   set(state.vlm_queues) | set(state.formatter_queues), arguments["groups"])
        except ValueError as exc:
            raise ToolInputError(str(exc)) from exc
        return {"resource_plan": arguments["groups"], "current_hardware": hardware}
    if name in {"create_vlm_queues", "create_formatter_queues"}:
        return await getattr(queues, name)(state)
    if name in {"ensure_redis", "start_docling"}:
        from agent.graph import ensure_redis, start_docling

        return await {"ensure_redis": ensure_redis, "start_docling": start_docling}[name](state)
    if name == "inspect_startup_resources":
        return {"current_hardware": await asyncio.to_thread(inspect_hardware), "resource_plan": []}
    if name in {"diagnose_redis", "start_redis_service"}:
        operation = diagnose_redis if name == "diagnose_redis" else start_redis_service
        result = await operation(state.config.get("redis_recovery", {}))
        return {"startup_diagnostics": {**state.startup_diagnostics, "redis": result}}
    if name == "abort_startup":
        return {"status": "failed", "errors": [*state.errors, "Startup stopped: " + arguments["reason"]]}
    raise ToolInputError(f"Unsupported startup tool: {name}")
