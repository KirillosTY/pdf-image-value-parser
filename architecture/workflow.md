**Current implementation update:** The approved formatting worker and relational
Postgres batch writer are documented in [formatting_and_storage.md](formatting_and_storage.md).
`db_status` is document-only; each transaction contains up to 50 whole, fully
formatted PDFs. The original walkthrough below describes the earlier coordinator
skeleton. Its service hooks remain unwired. The Docling wrapper is also stale:
it still references `on_document`, `iter_folder`, and `worker.run_id`, which the
current `DocumentWorker` no longer provides.

---

There are **two separate graphs**: a small graph that runs Docling extraction, and a larger graph intended to coordinate the whole pipeline. **They are not connected yet.** The following describes what the code currently contains, without treating its design choices as agreed requirements.

**1. The Docling graph**

[docling_graph.py](../src/docling/docling_graph.py) wraps `DocumentWorker` in a single LangGraph node:

```text
START → extract_folder → END
```

Its `ExtractionState` defines the inputs and summary outputs:

| Field | Purpose |
|---|---|
| `input_path` | PDF or directory to process |
| `output_dir` | Where output goes; defaults to `DEFAULT_IMAGES` |
| `config` | Settings passed into `ExtractionConfig` |
| `recursive` | Whether to search subdirectories; defaults to false |
| `run_id` | Returned worker run identifier |
| `counts` | Number of documents with each completion status |

The `extract_folder()` function:

1. Gets LangGraph’s custom stream writer.
2. Creates a `DocumentWorker`, passing that writer as its `on_document` callback.
3. Iterates through the worker’s documents and counts their statuses.
4. Returns the run ID, counts, and output directory.

There are two outputs: **per-document notifications streamed while processing**, and **a summary returned when the node finishes**. The graph state does not contain the extracted images or full manifests.

This wrapper runs the folder inside one node. It does not itself launch a background producer or orchestrate VLM work. Also, an input `run_id` is not passed into the worker; the returned ID comes from the worker.

**2. `agent/state.py`: the bookkeeping**

[state.py](../src/agent/state.py) defines Python dataclasses describing a run.

| Class | What it tracks |
|---|---|
| `RedisState` | Readiness, connection reference, whether this run owns Redis |
| `DoclingState` | Producer status and PDF counts/outcomes |
| `QueueState` | Waiting PDFs and work outstanding at each stage |
| `VlmState` | Instance configuration, status, image counts |
| `FormatterState` | Text-model configuration, worker status, image counts |
| `DocumentState` | Which images belong to a PDF and whether it was claimed |
| `ImageState` | One image’s stage, output references, errors, permanent record ID |
| `State` | All of the above, plus run identity, events, actions, and errors |
| `WorkerEvent` | A notification sent by a worker |

An image is expected to progress through:

```text
queued → vlm → analysis → mapping → writing → stored
                  Any active stage can fail → failed
```

Large payloads stay outside graph state. Fields such as `result_ref` and `normalized_ref` identify where those payloads are stored.

`State.__post_init__()` reconstructs nested dataclasses when state arrives as dictionaries, for example from a checkpoint.

**3. `agent/coordinator.py`: the transition rules**

[coordinator.py](../src/agent/coordinator.py) contains the implemented decision logic. It does not run models or write records.

Its main function is:

```python
apply_event(original_state, event)
```

It checks the event, copies the state, updates progress, and records actions that should happen next.

For example:

| Incoming event | State change | Action scheduled |
|---|---|---|
| `docling.pdf_queued` | Register PDF and its images | None |
| `docling.filled` | Mark startup threshold reached | Start VLM pool if needed |
| `docling.completed` | Mark producer finished | Start VLM for any remaining small batch |
| `vlm.pdf_claimed` | Move that PDF’s images into VLM processing | None |
| `vlm.image_completed` | Save raw-output reference; enter analysis | Start formatter if needed, schedule analysis |
| `analysis.completed` | Save normalized-output reference and chart type | Build database fields |
| `db_fields.completed` | Save mapped-fields reference | Schedule database write |
| `writer.committed` | Record permanent ID; mark image stored | Acknowledge PDF if all its images are terminal |

There are several checks:

- Events must belong to the current run.
- Repeated event IDs do not update counters again.
- Image events must refer to a registered PDF/image.
- Stages must occur in order.
- Required references must exist before advancing.

The copy matters: a validation error does not leave the original state half updated.

`refresh_progress()` recalculates outstanding-work counters from documents and image stages. `pipeline_is_finished()` requires Docling completion, every known image stored or failed, and no pending actions.

The **five-PDF startup threshold** is a configurable default already encoded here. Reaching five does not automatically emit an event: a worker adapter still needs to send `docling.filled`.

**4. `agent/hooks.py`: the unfinished service connections**

[hooks.py](../src/agent/hooks.py) names the operations the coordinator expects external code to perform:

- Connect to or start Redis.
- Launch Docling in the background.
- Receive and acknowledge worker events.
- Start VLM and formatter workers.
- Schedule analysis, field mapping, and writes.
- Acknowledge completed PDF jobs.
- Handle failures and clean up resources.

**Those operations currently raise `NotImplementedError`.** Their comments describe intended behavior, not working implementations.

Only two helper wrappers are implemented: `apply_worker_event()` and `pipeline_is_finished()`, both delegating to `coordinator.py`. The main graph calls the coordinator directly, so those wrappers are not used in its current path. `handle_worker_failure()` is also not dispatched by the current graph.

**5. `agent/graph.py`: the execution order**

[graph.py](../src/agent/graph.py) connects the bookkeeping and hooks:

```text
START
  ↓
initialize
  ↓
ensure_redis
  ↓
start_docling
  ↓
coordinate_pipeline ←─────────────────┐
  ↓                                   │
dispatch_action (until none remain)    │
  ↓                                   │
acknowledge_event                      │
  ↓                                   │
check progress ─── more work ──────────┘
  ↓ finished or fatal failure
finish_run
  ↓
END
```

Each pass receives **one event**, applies its transition, executes its planned actions one at a time, and acknowledges the event.

For example, a VLM result schedules formatting work. The graph then returns to listening for events; the formatter is intended to work separately and report back later.

The separate steps allow checkpointing between operations. `build_graph(checkpointer=...)` accepts a persistence implementation, but the exported `graph = build_graph()` does not supply one. Real recovery would also depend on the unfinished adapters safely handling repeated calls.

With valid input, **execution currently reaches `ensure_redis` and raises `NotImplementedError`**.

**6. `agent/__init__.py`: the export**

[__init__.py](../src/agent/__init__.py) simply imports and exports the compiled main graph.

So, the existing `agent` folder is a **deterministic event coordinator skeleton**. The substantive choices already expressed in it—startup threshold, separate formatting/mapping/writing stages, event acknowledgments, and recovery structure—are the parts to review together before connecting the workers.
