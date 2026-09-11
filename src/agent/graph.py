"""Coordinate workers through checkpointable event and action steps.

Worker adapters are still explicit placeholders. Use build_graph(checkpointer=...)
for persistence; adapters must make launches, enqueueing, and acknowledgments
idempotent because a crash can replay an action after its external side effect.
"""

from dataclasses import replace
from datetime import datetime, timezone

from langgraph.graph import END, START, StateGraph

from agent import hooks
from agent.coordinator import apply_event, initialize, pipeline_is_finished
from agent.state import State


async def ensure_redis(state: State) -> dict:
    """Wait for the Redis adapter to establish readiness."""
    update = await hooks.ensure_redis(state)
    redis = update.get("redis", state.redis)
    return {**update, "redis": replace(redis, status="ready")}


async def start_docling(state: State) -> dict:
    """Launch the producer before listening to its events."""
    update = await hooks.start_docling(state)
    docling = update.get("docling", state.docling)
    return {**update, "status": "running", "docling": replace(docling, status="running")}


async def coordinate_pipeline(state: State) -> dict:
    """Receive and apply exactly one event, recording its planned actions."""
    event = await hooks.wait_for_worker_event(state)
    return apply_event(state, event)


async def dispatch_action(state: State) -> dict:
    """Execute one recorded action before checkpointing the remaining actions."""
    action = state.pending_actions[0]
    if action in {"start_vlm_pool", "start_formatter_pool"}:
        update = await getattr(hooks, action)(state)
    else:
        update = await getattr(hooks, action)(state, state.current_event)
    return {**update, "pending_actions": state.pending_actions[1:]}


async def acknowledge_event(state: State) -> dict:
    """Acknowledge the event only after its state and action steps succeeded."""
    await hooks.acknowledge_worker_event(state, state.current_event)
    return {"current_event": None}


def route_actions(state: State) -> str:
    """Drain recorded actions before acknowledging the current event."""
    return "dispatch_action" if state.pending_actions else "acknowledge_event"


def route_progress(state: State) -> str:
    """Keep receiving events until completion or a fatal worker failure."""
    if state.status == "failed" or pipeline_is_finished(state):
        return "finish_run"
    return "coordinate_pipeline"


async def finish_run(state: State) -> dict:
    """Release owned resources before recording the terminal run status."""
    update = await hooks.finish_run(state)
    status = "failed" if state.status == "failed" else (
        "completed_with_errors" if state.errors else "completed"
    )
    return {
        **update,
        "status": status,
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }


def build_graph(*, checkpointer=None):
    """Compile the coordinator with an optional caller-owned checkpointer."""
    return (
        StateGraph(State)
        .add_node("initialize", initialize)
        .add_node("ensure_redis", ensure_redis)
        .add_node("start_docling", start_docling)
        .add_node("coordinate_pipeline", coordinate_pipeline)
        .add_node("dispatch_action", dispatch_action)
        .add_node("acknowledge_event", acknowledge_event)
        .add_node("finish_run", finish_run)
        .add_edge(START, "initialize")
        .add_edge("initialize", "ensure_redis")
        .add_edge("ensure_redis", "start_docling")
        .add_edge("start_docling", "coordinate_pipeline")
        .add_conditional_edges("coordinate_pipeline", route_actions,
                               ["dispatch_action", "acknowledge_event"])
        .add_conditional_edges("dispatch_action", route_actions,
                               ["dispatch_action", "acknowledge_event"])
        .add_conditional_edges("acknowledge_event", route_progress,
                               ["coordinate_pipeline", "finish_run"])
        .add_edge("finish_run", END)
        .compile(name="Main pipeline agent", checkpointer=checkpointer)
    )


graph = build_graph()
