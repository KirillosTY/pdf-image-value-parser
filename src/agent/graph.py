"""Expose MainAgent startup tools and the extraction-to-SQL flow in Studio.

Named nodes route and publish ready work to live consumers. A shared thinking
setting controls both routing branches; the writer joins complete manifests.
"""

from dataclasses import replace
from datetime import datetime, timezone

from langgraph.graph import END, START, StateGraph

from agent import hooks, stages, startup_orchestration
from agent.progress_messages import show_progress
from agent.startup import prepare_agent
from agent.startup_tools import STARTUP_TOOL_NAMES
from agent.state import State


async def ensure_redis(state: State) -> dict:
    """Verify queue storage before creating any model queues."""
    update = await hooks.ensure_redis(state)
    redis = update.get("redis", state.redis)
    return {**update, "redis": replace(redis, status="ready")}


async def start_docling(state: State) -> dict:
    """Start the runtime; batch extraction waits for the following Docling node."""
    update = await hooks.start_docling(state)
    docling = update.get("docling", state.docling)
    return {**update, "status": "running", "docling": replace(docling, status="running")}


async def finish_run(state: State) -> dict:
    """Record the actual outcome and release owned resources after SQL settles."""
    update = await hooks.finish_run(state)
    status = "failed" if state.status == "failed" else (
        "completed_with_errors" if state.errors else "completed"
    )
    show_progress(state.config, f"Pipeline finished: {status.replace('_', ' ')}.")
    return {**update, "status": status,
            "completed_at": datetime.now(timezone.utc).isoformat()}


def build_graph(*, checkpointer=None):
    """Expose MainAgent-selected startup tools followed by the document stages."""
    builder = (
        StateGraph(State)
        .add_node("prepare_agent", prepare_agent)
        .add_node("initialize", stages.initialize_staged_run)
        .add_node("main_agent_startup", startup_orchestration.select_startup_tool)
        .add_node("startup_tool_result", lambda state: {})
        .add_node("startup_failed", startup_orchestration.record_startup_failure)
        .add_node("docling", stages.docling)
        .add_node("await_ready_work", stages.await_ready_work)
        .add_node("main_agent_route_images", stages.main_agent_route_images)
        .add_node("route_images_directly", stages.route_images_directly)
        .add_node("vlm", stages.vlm)
        .add_node("main_agent_route_outputs", stages.main_agent_route_outputs)
        .add_node("route_outputs_directly", stages.route_outputs_directly)
        .add_node("formatter", stages.formatter)
        .add_node("map_database_fields", stages.map_database_fields)
        .add_node("write_database", stages.write_database)
        .add_node("finish_run", finish_run)
        .add_edge(START, "prepare_agent")
        .add_edge("prepare_agent", "initialize")
        .add_edge("initialize", "main_agent_startup")
        .add_edge("startup_failed", END)
        .add_conditional_edges("docling", stages.choose_image_route, {
            "thinking_enabled": "main_agent_route_images",
            "direct": "route_images_directly",
        })
        .add_conditional_edges("await_ready_work", stages.choose_image_route, {
            "thinking_enabled": "main_agent_route_images",
            "direct": "route_images_directly",
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
            "more_work": "await_ready_work", "finished": "finish_run",
        })
        .add_edge("finish_run", END)
    )
    tool_nodes = [*STARTUP_TOOL_NAMES, "invalid_startup_tool"]
    for name in tool_nodes:
        builder.add_node(name, startup_orchestration.execute_selected_tool)
        builder.add_edge(name, "startup_tool_result")
    builder.add_conditional_edges("startup_tool_result", startup_orchestration.after_startup_tool, {
        "main_agent_startup": "main_agent_startup", "docling": "docling",
        "startup_failed": "startup_failed",
    })
    builder.add_conditional_edges("main_agent_startup", startup_orchestration.selected_startup_node, {
        name: name for name in [*tool_nodes, "main_agent_startup", "startup_failed"]
    })
    return builder.compile(
        name="MainAgent document pipeline", checkpointer=checkpointer,
    ).with_config({"recursion_limit": 1_000_000})


graph = build_graph()
