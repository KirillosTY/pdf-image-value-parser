"""Provide state-scoped tools for the MainAgent's orchestration loop.

The model selects tools and supplies decisions. Python enforces approved routing,
resource limits and event ordering before any worker side effect is permitted.
"""

from dataclasses import asdict

from jsonschema import Draft202012Validator

from agent import hooks, queues
from agent.coordinator import apply_event, pipeline_is_finished
from agent.resources import compatible_groups, validate_resource_plan
from agent.state import State


class ToolInputError(ValueError):
    """Identify invalid model decisions that can be corrected without side effects."""


def _tool(name, description, properties=None):
    properties = properties or {}
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object", "properties": properties,
                "required": list(properties), "additionalProperties": False,
            },
        },
    }


def _routes(keys, models, fixed=None):
    return {
        "type": "object",
        "properties": {
            key: {"type": "string", "enum": [fixed[key]] if fixed else sorted(models)}
            for key in keys
        },
        "required": list(keys), "additionalProperties": False,
    }


def available_tools(state: State) -> list[dict]:
    """Offer only operations whose prerequisites are satisfied in this state."""
    config = state.config
    vlms = {key for key, model in config["models"].items() if "vlm" in model["roles"]}
    formatters = {
        key for key, model in config["models"].items() if "formatter" in model["roles"]
    }
    if not state.VLM_INPUT:
        charts = list(dict.fromkeys([*config["docling_charts"], "unknown"]))
        fixed = None
        if not config["vlm_think_sorting"]:
            fixed = {chart: config["chart_matcher"].get(chart, config["fallback_vlm"])
                     for chart in charts}
        schema = _routes(charts, vlms, fixed)
        if config["fallback_vlm"]:
            schema["properties"]["unknown"]["enum"] = [config["fallback_vlm"]]
        return [_tool("set_vlm_routes", "Choose the best VLM for every chart type. Respect fixed routes.",
                      {"routes": schema})]
    if not state.FORMATTER_INPUT:
        selected = sorted(vlms if config["vlm_think_sorting"] else set(state.VLM_INPUT.values()))
        fixed = None
        if not config["formatter_think_sorting"]:
            fixed = {key: config["format_matcher"].get(key, config["fallback_formatter"])
                     for key in selected}
        return [_tool("set_formatter_routes", "Match each selected VLM's output to a formatter and the approved schema.",
                      {"routes": _routes(selected, formatters, fixed)})]
    if state.redis.status != "ready":
        return [_tool("ensure_redis", "Verify Redis connectivity before creating run queues.")]
    tools = []
    if not state.vlm_queues:
        tools.append(_tool("create_vlm_queues", "Create one Redis queue per selected VLM."))
    if not state.formatter_queues:
        tools.append(_tool("create_formatter_queues", "Create one Redis queue per selected formatter."))
    if tools:
        return tools
    if not state.resource_plan:
        return [_tool("schedule_models", "Choose model groups and their order. Each group runs concurrently; groups take turns draining bounded batches. Include every queued model once. Memory limits are enforced.", {
            "groups": {"type": "array", "minItems": 1, "items": {
                "type": "array", "minItems": 1, "uniqueItems": True,
                "items": {"type": "string", "enum": sorted(set(state.vlm_queues) | set(state.formatter_queues))},
            }},
        })]
    if state.docling.status == "idle":
        return [_tool("start_docling", "Start the Docling producer and run lease. The graph processes downstream stages after the manifest minimum or producer completion.")]
    if state.pending_actions:
        name = state.pending_actions[0]
        descriptions = {
            "start_vlm_pool": "Release the manifest gate and enable VLM workers under the approved resource plan.",
            "start_formatter_pool": "Ensure the formatter supervisor is available.",
            "analyze_vlm_result": "Read this event's saved VLM output and context and route it to its formatter queue.",
            "build_db_fields": "Validate the formatted result and map fields to the approved SQL schema.",
            "write_chart_record": "Submit the mapped image to the whole-document writer; writes wait for document eligibility and batch thresholds.",
            "acknowledge_pdf": "Acknowledge this completed document claim while preserving diagnostics.",
            "handle_worker_failure": "Retain terminal failure evidence and acknowledge its document.",
        }
        return [_tool(name, descriptions[name])]
    if state.current_event:
        return [_tool("acknowledge_event", "Acknowledge the worker event after all its actions succeeded.")]
    if state.status == "failed" or pipeline_is_finished(state):
        return [_tool("finish_run", "Flush eligible tail documents, record the actual outcome, and close owned workers.")]
    return [_tool("wait_for_worker_event", "Await the next durable worker event and update progress. This blocks asynchronously until work is ready.")]


def observation(state: State) -> dict:
    """Show bounded progress and the current event without copying every image."""
    result = {
        "run_id": state.run_id, "status": state.status,
        "VLM_INPUT": state.VLM_INPUT, "FORMATTER_INPUT": state.FORMATTER_INPUT,
        "vlm_queues": state.vlm_queues, "formatter_queues": state.formatter_queues,
        "resource_plan": state.resource_plan, "queues": asdict(state.queues),
        "docling_status": state.docling.status,
        "pending_actions": state.pending_actions,
        "current_event": asdict(state.current_event) if state.current_event else None,
        "recent_errors": state.errors[-3:],
    }
    if state.vlm_queues and state.formatter_queues and not state.resource_plan:
        result["suggested_resource_groups"] = compatible_groups(
            state.config, state.current_hardware,
            set(state.vlm_queues) | set(state.formatter_queues),
        )
    return result


async def execute_tool(state: State, name: str, arguments: dict) -> dict:
    """Validate a decision before invoking its adapter; propagate operational errors."""
    spec = next((tool["function"] for tool in available_tools(state)
                 if tool["function"]["name"] == name), None)
    if spec is None:
        raise ToolInputError(f"Tool {name!r} is not available in the current state")
    errors = list(Draft202012Validator(spec["parameters"]).iter_errors(arguments))
    if errors:
        raise ToolInputError(errors[0].message)
    if name == "set_vlm_routes":
        return {"VLM_INPUT": arguments["routes"]}
    if name == "set_formatter_routes":
        return {"FORMATTER_INPUT": arguments["routes"]}
    if name == "schedule_models":
        try:
            validate_resource_plan(
                state.config, state.current_hardware,
                set(state.vlm_queues) | set(state.formatter_queues), arguments["groups"],
            )
        except ValueError as exc:
            raise ToolInputError(str(exc)) from exc
        return {"resource_plan": arguments["groups"]}
    if name in {"create_vlm_queues", "create_formatter_queues"}:
        return await getattr(queues, name)(state)
    if name in {"ensure_redis", "start_docling", "finish_run"}:
        # Share lifecycle behavior with the explicitly agent-free graph.
        from agent.graph import ensure_redis, finish_run, start_docling

        return await {"ensure_redis": ensure_redis, "start_docling": start_docling,
                      "finish_run": finish_run}[name](state)
    if name == "wait_for_worker_event":
        return apply_event(state, await hooks.wait_for_worker_event(state))
    if name == "acknowledge_event":
        await hooks.acknowledge_worker_event(state, state.current_event)
        return {"current_event": None}
    if name in {"start_vlm_pool", "start_formatter_pool"}:
        update = await getattr(hooks, name)(state)
    else:
        update = await getattr(hooks, name)(state, state.current_event)
    return {**update, "pending_actions": state.pending_actions[1:]}
