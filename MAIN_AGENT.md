# Main agent orchestration contract

The coordinator implements state transitions and event routing; external worker
adapters remain unfinished. The main agent owns the state of the whole run; workers
report events that it uses to update their respective state sections.

## Execution flow

1. Ensure Redis is running and reachable before starting Docling. Start a local
   Redis server when configured to manage it; otherwise connect to the configured
   server. Record whether this run owns the server process.
2. Start Docling as a background producer. It extracts PDFs, stores their images
   and manifests in Redis, and publishes ready PDF jobs.
3. Docling signals `filled` once at least five successfully processed PDFs are
   waiting in this run's VLM queue. Count PDFs, not images or all historical
   stream entries. Update the Docling state section and start the VLM consumer.
   Docling continues processing the remaining PDFs concurrently.
4. Docling signals `completed` when it has exhausted its input and finished all
   queue publications. This also starts the VLM if fewer than five PDFs were
   produced. If there are no ready PDFs, there is no VLM work to start.
5. VLM workers consume queued PDFs and extract raw observations from their images.
   Each image result becomes available to the main agent immediately; storage
   does not wait for the entire PDF or Docling run to finish.
6. The main agent queues each VLM result for separate formatting workers. A
   configured text model resolves chart type and normalizes the output. Code
   validates the result against its schema, with bounded model repair attempts
   before recording unresolved or invalid output as an explicit failure.
7. The database-fields step in the formatting worker maps the validated result
   into a permanent record and validates required fields.
   The writer commits that record and reports success or failure to the main
   agent. Field mapping does not imply creating or altering database tables for
   each image.
8. Finish the run only after Docling has completed, the queues are drained, no
   VLM/analysis/storage work is in flight, and every item has a recorded terminal
   outcome. Distinguish clean completion from completion with item errors.

## State owned by the main agent

| Section | Fields and meaning |
| --- | --- |
| Run | `run_id`, input/configuration, `status` (`starting`, `running`, `completed`, `completed_with_errors`, `failed`), timestamps, errors |
| Redis | `status` (`starting`, `ready`, `failed`), connection reference, `managed_by_run` |
| Docling | `status` (`idle`, `running`, `filled`, `completed`, `failed`), processed/queued/failed/no-assets PDF counts, `filled_at`, `completed_at` |
| Queues | This run's waiting PDFs, in-flight PDFs/images, pending analysis results and writes |
| VLM | `status` (`idle`, `running`, `draining`, `completed`, `failed`), `vlm_instances`, `requests_per_instance`, instance endpoints, active instances, processed/failed image counts |
| Formatter | Status, text-model name/endpoint, `max_workers`, active workers, processed/failed image counts |
| Analysis | Per-image result reference, resolved chart type, validation status and error |
| Storage | Per-image field-mapping/write status, permanent record ID, stored/failed counts |

Keep image bytes and full result payloads outside graph state. State carries
references and summaries, with per-item records keyed by run, document attempt,
asset, and image ordinal. All queue work and counters must be scoped to the run.

`filled` is a one-time startup signal: the Docling worker remains active in this
state until `completed`. A shrinking queue does not undo the signal or stop the
VLM. `completed` means the producer has finished, not that the whole pipeline has
finished or that every PDF succeeded. Preserve per-PDF outcomes separately.

## Worker event contract

Every event carries `event_id`, `run_id`, source, timestamp, and relevant document
attempt/asset/image identifiers. The coordinator applies duplicate events only
once so redelivery cannot double-count work.

| Event | Coordinator action |
| --- | --- |
| `docling.pdf_queued` | Record the successfully published PDF and update waiting-work accounting |
| `docling.pdf_finished` | Record an unqueued PDF's `no_assets` or `failed` outcome |
| `docling.filled` | Latch startup readiness and start the VLM pool if it is not already started |
| `docling.completed` | Mark the producer finished and release any remaining small batch |
| `vlm.pdf_claimed` | Move the PDF's expected images from waiting to in-flight VLM work |
| `vlm.image_completed` | Record the result reference and schedule chart analysis |
| `analysis.completed` | Pass the resolved chart type and validated data to field mapping |
| `db_fields.completed` | Schedule the permanent write |
| `writer.committed` | Record the permanent ID and mark the image stored |
| `worker.item_failed` | Record a terminal image error after worker-side retries are exhausted |
| `worker.failed` | Record a stage-level failure; do not report successful run completion |

Only publish a ready PDF after its manifest and accepted image data have been
stored successfully. A PDF with no accepted images has a terminal no-assets
outcome and does not contribute toward the five-PDF threshold.

Queue claims must assign work to one consumer at a time. Preserve unfinished
work for recovery; acknowledge a PDF job only after all its images have durable
terminal outcomes. Writes must be idempotent for the same image attempt, so a
retry after a commit cannot create duplicate permanent records. Unknown chart
types or invalid output receive an explicit unresolved/failed outcome rather
than an invented field mapping.

## VLM instances and formatting workers

`vlm_instances` controls independently running vision-model instances;
`requests_per_instance` limits concurrent requests to each instance. Both default
to one. Instances claim distinct Redis jobs, subject to memory/compute capacity.
Automatic capacity detection and adaptive scaling remain future work. Repeated
readiness signals reuse the existing pool.

Formatting workers use a separate configurable text-model endpoint and their
own worker limit. They may share one loaded text model. The coordinator queues
work without running inference inline. Bounded result/write queues provide
backpressure. Preserve raw output and never invent values to satisfy a schema.
Code validation checks structure; it cannot establish extraction accuracy.
The formatter must account for each extracted observation as mapped or unmapped,
retaining source references and reasons in `unmapped_observations`. The writer
must permanently save the original extraction and unresolved observations along
with the formatted record. This guards against formatting losses, not omissions
the VLM made while reading the image.

## Current implementation boundaries

The main agent is split into four files:

- `src/agent/state.py`: run state, worker state sections, per-image progress,
  and the worker event envelope.
- `src/agent/hooks.py`: named asynchronous worker/lifecycle adapters and a
  completion predicate, with pseudocode for unfinished external operations.
- `src/agent/coordinator.py`: configuration checks, atomic event validation,
  duplicate-event handling, per-document/image progress, pending action planning,
  and completion checks.
- `src/agent/graph.py`: Redis readiness → background Docling launch → event
  processing → action dispatch → event acknowledgment, repeating until finalization.
  Each event and each action is a separate graph step.

The graph compiles for inspection. Invocation raises `NotImplementedError` at
the first unwired hook; it does not start services or claim successful work.

`src/redis/state.py` provides image/manifest storage and a `pdf:ready` stream,
and Docling calls these helpers. It does not yet provide the run-scoped readiness
events consumed by the coordinator. `src/vlm/vlm.py` provides a single-image model call,
not a queue consumer. It now returns raw text without a `response_format` or
schema argument. Its prompt asks for visible observations and uncertainties
without limiting the chart type to a fixed list. Persisting that raw text and
implementing model-backed formatting remain future work.
`src/schema_format.py` defines chart schemas and initial
Docling-based routing; post-VLM analysis must reconcile the final chart type
with those schemas. `src/db/db.py` is empty. Redis lifecycle management, the
consumer pool, field mapper, durable writer, and durable event transport remain
to be implemented.

## Adapter and recovery requirements

`docling.pdf_queued` must carry `document_attempt_id` and a nonempty `image_keys`
list identifying every accepted image. Use stable, run-unique keys incorporating
the document attempt, asset ID, and image ordinal. Image events carry the matching
`document_attempt_id` and `image_key`. Producer completion must follow all PDF
notifications; a PDF claim notification must precede its image results.

`analysis.completed` requires the normalized payload reference, resolved chart
type, and `unmapped_observations_ref` (pointing to an empty list when all findings
were mapped). `writer.committed` requires the permanent `record_id`. The writer
must emit it only after raw, formatted, and unmapped data have been committed.
The coordinator validates the event envelope and sequence; payload validation
is still the responsibility of the future formatting adapter.

Adapters return typed state-section updates and must return promptly after
launching/scheduling. They must not mutate input state or overwrite coordinator
bookkeeping. Calls may be replayed after a crash: use `run_id` for pool launch
idempotency and `(run_id, event_id, action_name)` for scheduled work. A PDF is
acknowledged only after all its images are stored or terminally failed, and the
acknowledgment adapter must persist terminal errors before releasing the claim.

Use `build_graph(checkpointer=...)` with a durable checkpointer, a stable
`configurable.thread_id`, and `durability="sync"` when invoking for durable
recovery. Resume an interrupted run with `ainvoke(None, config, durability="sync")`.
The default exported graph does not configure persistence. Set `recursion_limit`
for the expected event volume: each event takes multiple graph steps. The current
per-item state and processed-event list grow with the run; large-run compaction
is not implemented.

Adapter exceptions leave the graph at the failed step for inspection/resume;
they do not masquerade as successful worker events. Automatic retry, cancellation
cleanup, and worker-death detection are not implemented. Explicit `worker.failed`
events route to cleanup and finalization with run status `failed`.

Focused tests cover readiness thresholds, small/empty runs, event ordering,
duplicates, image failures, raw/unmapped reference retention, and resuming a
failed action with an in-memory checkpointer and fake worker adapters. They do
not start Redis, load models, or write a real database.
