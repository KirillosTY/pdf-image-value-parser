"""Coordinate workers through checkpointable event and action steps.

Worker adapters are still explicit placeholders. Use build_graph(checkpointer=...)
for persistence; adapters must make launches, enqueueing, and acknowledgments
idempotent because a crash can replay an action after its external side effect.

The compiled graph is the pipeline's map: coordinate_pipeline applies one worker
event, then the actions it scheduled drain one per pass through the node that owns
each — ensure_pools, analyze, map_fields, write, acknowledge_pdf — with every edge
labeled by the action it dispatches. That labeling does not change what executes.
"""

This shit text, more compact and simple terms jesus christ 

graph.add_conditional_edges("coordinate_pipeline", route_actions,
                                {**DISPATCH, DRAINED: "acknowledge_event"})
explain this **dispatch