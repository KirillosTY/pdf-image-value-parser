Some fixes to the whole thing, propose a plan. the document schematics and all are derived from runs. This happens via database inquiry.



Everything listed below is desired workflow. go through 1 section at a time. A section is marked by ----------;

first a read me is needed for LLM:

This read me instructs the LLM to fill out information needed for the MainAgent later on:

config.py {

    GPU= information about
    GPU_MEM =
    CPU=
    RAM =
    other criteria for orcherstrating the models...

    The LLM model needs to ask what type of charts the user is focusing on then it suggest models to the users and it should write the information about the models. Context should include parameter count, expected output and so on.

    main_agent = the one responsible for ochestrating and deciding which chart goes to which model.
    models = ["qwen3.8",...]
    formatters = ["Qwen", "DePlot","ChartGemma", "NuExtract",...]

    think_sorting= boolean, tells if we should use mainagent during the run to match images to right vlm, for just map out docling output types to targeted chart types. Set to false if only 1 model available. if set to false 1 with multiple models then chart_matcher should contain which docling chart types to automatically match to which models, and which vlm output to match to which formatter in format_matcher

    qwen3.8_context = "..."
    dePlot_context = "..."
    NuExtract_context = "..."
    charts_targeted ="..."
    docling_chart="..."
    chart_matcher =["bar_chart(docling):"qwen3.5", ...]
    format_matcher=["qwen3.5":"NuExtract",...]

    queue_count = specifies how many times a failed image will be requeued.

    approve_with_fails = in case manifest has failed images do we allow it to proceed to database.

}
----------

read me also instructs the configuring LLM to define what type of SQL schema and tables are desired, then write it to appropriate folder;

these will be matched with formatters etc, open to discussion.

----------

Main agent starts: it's system prompt contains = what it is intended to do and
it loads schematics from the config.py to its system prompt.
It reads what different models we have and the context of the models from config.py with the charts too. so config py should have descriptive amount of text so LLMs know what to expect.

This final context about what is supposed to do, models etc, become it's system prompt.

----------

MainAgent now matches the charts to optimal VLM models

This is in MainAgentState

VLM_INPUT = MAP [

    key: charts_type:str
    value vlm: str

]


----------

MainAgent now matches the VLM output to optimal formatter models

This is in MainAgentState

Formatter_INPUT = MAP [

    key: vlm:str
    value: formatter: str

]


----------


MainAgent uses tool that creates 1 queue for each model used in VLM_INPUT (if only 1 model in use it only uses that 1 model and there is just 1 queue)
for each value it creates a redis queue in the run_queue:

qwen3.8_queue: images

Qwen2.5_queue...
....



----------

MainAgent uses tool that creates 1 queue for each model used in FORMATTER_INPUT (if only 1 model in use it only uses that 1 model and there is just 1 queue)
for each value it creates a redis queue in the run_queue:

NuExtract_queue: vlm output and context


....

----------


Main Agent uses tool to start docling node
Docling node starts adding manifests to redis, new cap is 50 max and minimal of 20 before mainAgent continues.

----------

In case think_sorting is true: MainAgent opens 1 manifest at time and starts sorting the picture to the right vlm queue, based on the total context: caption, mentions and nearby, confidence. (we need to allow top 3 confidence to show)


important if there is only vlm model then parameter think_sorting is false MainAgentState. This means that when docling puts out manifests it's images are just automatically added to the queue and thinks proceeds normally.

if think_sorting is set to false with multiple models: use the chart_matcher to automatically sort images to right queue.

----------

MainAgent uses system information and model information(itself included), to determine how many VLMs it can fire up at once.
then sets the limit accordingly.

MainAgent fires worker that manages which models run: Model loading and switching is intensive, thus it should empty one model before queue the next, _if_ all don't fit at once else load all to run simultaneously. (or maybe run max 50)(open to discussion)

VLMs poll their own queues only.

MainAgent fires a worker to poll when images are ready to notify(all polls can be async or something like that discuss best option with me)

----------

 MainAgent opens image at a time and starts sorting the picture to the right formatter, based on the vlm output.

(In case a formatter has failed to process an image vlm tries to requeue it to start of list, adds a +1 to it's queue_count)

important if there is only formatter model then parameter think_sorting is false MainAgentState. This means that when docling puts out manifests it's images are just automatically added to the queue and thinks proceeds normally.

if think_sorting is set to false with multiple models: use the format_matcher to automatically sort images to right queue.

----------

Formatter poll their own queues and once image is complete and formatted

(In case formatting fails, the image is added again to the start of queue with increasing queue_Count)

MainAgent fires a worker that checks if a single manifest is complete, in the case it is sent to writer queue

Writer fires when no more images are in other queues, or it's manifest queue has reached at least 50.

----------
Current implementation (2026-09-16; supersedes older workflow details above)

- Setup is conducted by the coding assistant on first use or when RERUN_SETUP
  is set in src/config.py; SETUP_COMPLETED records completion.
- One think_sorting setting controls both routing decisions. One shared
  MainAgent selects VLM and formatter queues, one decision at a time.
- Processing starts with the first published manifest. Model consumers stay
  active, and formatters process available outputs before the VLM batch finishes.
- Named graph nodes expose routing, VLM/formatter queue handoffs, mapping and SQL.
  They do not impose whole-batch stage barriers. The repeat edge leads to
  await_ready_work, not back to Docling extraction.
- Each complete manifest is committed immediately in one transaction. With
  approve_with_fails=False, an exhausted image failure blocks only its manifest;
  other manifests continue. Failure evidence is retained.
- Local formatters request schema-constrained JSON; the parser safely accepts
  a single Markdown fence while retaining all data/schema validation.
- Routine Redis/queue/lifecycle operations no longer require model decisions.

See ../MAIN_AGENT.md for the current implementation and recovery instructions.
Tests and live-model checks remain paused. Static inspection and graph export
are not an end-to-end verification or a throughput measurement.

Remaining limitations:
- The CLI still has no durable checkpointer; use the graph API for checkpointed
  recovery. This topology requires a new run/thread.
- The two added VLMs' under-4-GB RAM requirement remains unmeasured and is not
  enforced by the external Ollama endpoint.
- Service readiness, native-tool behavior, managed serving and model accuracy
  have not been rechecked during this change.
