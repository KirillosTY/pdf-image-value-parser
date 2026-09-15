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
What is still missing?

Correction (2026-09-15): the earlier statement that all sections were implemented
was inaccurate. The worker pipeline existed, but the central tool-calling
MainAgent had been replaced by deterministic orchestration with optional routing
requests.

The MainAgent now operates through named setup/tool subgraphs and explicit
per-image routing nodes. src/tools/tools.py provides native setup tools; image
and result routing use native queue-selection tools from agent/model.py.
The Studio graph shows Docling → MainAgent/direct image routing → VLM →
MainAgent/direct output routing → formatter → database mapping → SQL writing.
Actual processing occurs inside these stage nodes; only Docling and the lease
run in the background. Batches retain Redis queues, retries and SQL semantics.
Setup tool decisions and execution remain separate checkpointable nodes. The selected model is the installed
qwen3-abliterated:latest, as clarified by the user. New test code is retained, but
testing and live-model checks were paused at the user's request; this path is
not yet verified end to end. See ../MAIN_AGENT.md for behavior and limitations.

Remaining gaps and deployment considerations (updated 2026-09-15):

Code
- Checkpointer not wired: src/agent/__main__.py calls build_graph() with no
  checkpointer, so graph state has no crash recovery (only Redis/runtime leases
  recover). Biggest real item.
- approve_with_fails supports only False/"omit"; no "proceed-with-fails" mode.
- The active graph waits for Docling batches and executes each downstream stage
  directly. Legacy worker-event helpers remain outside the active Studio graph.
- The explicit-stage topology requires a new run/thread; old coordinator
  checkpoints have no migration to the new stage graph.

Config / deployment
- CONFIG retains the local chart workload (manifest_minimum=1,
  writer_batch_size=1) and selected MainAgent, with three VLMs and three
  formatters after the user's requested additions. Both routing-thinking flags
  are enabled. See configuration/additional-models.md for candidates and setup.
- The two added VLMs target under 4 GB RAM each; peak RAM is unverified and the
  external Ollama path does not enforce a per-model memory cap. Testing remains
  paused, so this requirement has not yet been demonstrated.
- Existing local service setup is documented in configuration/test-pdf.md.
  Service readiness was not rechecked during this implementation.
- MainAgent endpoint context allocation and native tool behavior remain unverified.
- Managed-serving path (serving="managed", launch_command, RAM/VRAM estimates)
  is untested; current fixture is all external/Ollama.

Confirmed built (not gaps): VLM/formatter routing maps, per-model Redis queues,
docling gate (min/cap) + top-3 confidence, retry/priority-requeue, memory-group
scheduling + model load/switch, whole-document batch writer, recovery leases.
