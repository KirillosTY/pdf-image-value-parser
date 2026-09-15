"""Expose the actual extraction, routing, formatting and SQL flow in Studio.

Docling produces bounded batches in the background. Every downstream operation
runs in its named graph node; thinking flags select visible routing branches.
"""

from dataclasses import replace
from datetime import datetime, timezone

from langgraph.graph import END, START, StateGraph

from agent import hooks, stages
from agent.main_agent import build_tool_step
from agent.queues import create_formatter_queues, create_vlm_queues
from agent.routing import match_formatter_models, match_vlm_models
from agent.startup import prepare_agent
from agent.state import State


async def ensure_redis(state: State) -> dict:
    """Verify queue storage before creating any model queues."""
    update = await hooks.ensure_redis(state)
    redis = update.get("redis", state.redis)
    return {**update, "redis": replace(redis, status="ready")}


async def start_docling(state: State) -> dict:
    """Start the Docling producer and run lease; downstream work stays in the graph."""
    update = await hooks.start_docling(state)
    docling = update.get("docling", state.docling)
    return {**update, "status": "running", "docling": replace(docling, status="running")}


async def finish_run(state: State) -> dict:
    """Record the actual outcome and release owned resources after SQL settles."""
    update = await hooks.finish_run(state)
    status = "failed" if state.status == "failed" else (
        "completed_with_errors" if state.errors else "completed"
    )
    return {**update, "status": status,
            "completed_at": datetime.now(timezone.utc).isoformat()}


def build_graph(*, checkpointer=None):
    """Compile a descriptive stage graph with separately visible thinking branches."""
    return (
        StateGraph(State)
        .add_node("prepare_agent", prepare_agent)
        .add_node("initialize", stages.initialize_staged_run)
        .add_node("match_vlm_models", build_tool_step("set_vlm_routes", match_vlm_models))
        .add_node("match_formatter_models", build_tool_step("set_formatter_routes", match_formatter_models))
        .add_node("ensure_redis", build_tool_step("ensure_redis", ensure_redis))
        .add_node("create_vlm_queues", build_tool_step("create_vlm_queues", create_vlm_queues))
        .add_node("create_formatter_queues", build_tool_step("create_formatter_queues", create_formatter_queues))
        .add_node("schedule_models", build_tool_step("schedule_models", stages.schedule_models))
        .add_node("start_docling", build_tool_step("start_docling", start_docling))
        .add_node("docling", stages.docling)
        .add_node("main_agent_route_images", stages.main_agent_route_images)
        .add_node("route_images_directly", stages.route_images_directly)
        .add_node("vlm", stages.vlm)
        .add_node("main_agent_route_outputs", stages.main_agent_route_outputs)
        .add_node("route_outputs_directly", stages.route_outputs_directly)
        .add_node("formatter", stages.formatter)
        .add_node("map_database_fields", stages.map_database_fields)
        .add_node("write_database", stages.write_database)
        .add_node("finish_run", build_tool_step("finish_run", finish_run))
        .add_edge(START, "prepare_agent")
        .add_edge("prepare_agent", "initialize")
        .add_edge("initialize", "match_vlm_models")
        .add_edge("match_vlm_models", "match_formatter_models")
        .add_edge("match_formatter_models", "ensure_redis")
        .add_edge("ensure_redis", "create_vlm_queues")
        .add_edge("create_vlm_queues", "create_formatter_queues")
        .add_edge("create_formatter_queues", "schedule_models")
        .add_edge("schedule_models", "start_docling")
        .add_edge("start_docling", "docling")
        .add_conditional_edges("docling", stages.choose_image_route, {
            "thinking_enabled": "main_agent_route_images",
            "direct": "route_images_directly",
            "no_more_images": "write_database",
        })
        .add_edge("main_agent_route_images", "vlm")
        .add_edge("route_images_directly", "vlm")
        .add_conditional_edges("vlm", stages.choose_formatter_route, {
            "thinking_enabled": "main_agent_route_outputs",
            "direct": "route_outputs_directly",
        })
        .add_edge("main_agent_route_outputs", "formatter")
        .add_edge("route_outputs_directly", "formatter")
        .add_edge("formatter", "map_database_fields")
        .add_edge("map_database_fields", "write_database")
        .add_conditional_edges("write_database", stages.after_database, {
            "next_batch": "docling", "finished": "finish_run",
        })
        .add_edge("finish_run", END)
        .compile(name="MainAgent document pipeline", checkpointer=checkpointer)
    )


graph = build_graph()
