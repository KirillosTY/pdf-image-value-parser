# New LangGraph Project

## Scientific PDF extraction

The extraction worker is in `src/docling/docling_tool.py`. It processes local PDFs
sequentially, saves chart/table PNGs and a JSON manifest for each PDF, then calls
an optional notification callback before advancing. Redis and numerical-data
extraction belong to the later workflow and are not implemented here.

```bash
uv sync
uv run python -m figure_parser.docling_tool /path/to/pdfs --device cuda
```

Use `--device mps` on an Apple Silicon Mac, `cpu` for CPU processing, or `auto`
to let Docling choose. The first conversion downloads Docling's model weights.
There are no external metadata lookups or VLM calls.

Outputs are stored under
`images/<pdf_name>__<hash_prefix>/<attempt_id>/`. PNG filenames identify the
document, page, figure/table label when available, and asset ID. The manifest
contains the PDF's SHA-256, source path, run/attempt IDs, document metadata,
page numbers, captions, explicitly referencing paragraphs from across the
paper, and nearby paragraphs. Explicit figure/table mentions join continuation
blocks and existing equation text across columns/pages, keeping the surrounding
paragraph when sentence boundaries are uncertain. Joined mentions include
`item_refs` for their source blocks and all source page numbers. Existing LaTeX
is preserved; missing equation text is marked `[equation text unavailable]`.
No formula extraction model is enabled. Captions and nearby text keep their
existing extraction behavior. PNG crops use
Docling's detected figure/table regions, excluding separately detected captions.
Figures are kept whole as detected; individual panels are not split intentionally.

Missing bibliographic fields remain `null` or empty. Authors from a PDF's plain
Author field may remain an unsplit string in the authors list. Metadata evidence
is recorded at document level. Unlinked captions can be recovered from explicit
labels and nearby coordinates; these links are marked `spatial_fallback`.

Statuses are `processing`, `completed`, `no_assets`, `partial_failure`,
`failed_processing`, or `interrupted`. Every invocation creates a new attempt;
there is no automatic skipping, retry scheduler, or cache. A hard-killed process
may leave `processing`, identifiable by its run and attempt IDs. Partial/failed
PDFs have `ready: false`; the worker continues with the next PDF.

To connect Redis later, supply `on_document(event)` to `DocumentWorker`. The
callback receives a manifest path and completion status after saving finishes.
If the callback raises, the manifest records `notification_error`; delivery is
not retried automatically. Existing manifests can be used by your future retry
or notification implementation.

For LangGraph Studio, run `uv run langgraph dev` and select `extract_documents`:

```json
{"input_path": "/absolute/path/to/pdfs", "config": {"device": "cuda"}}
```

The compiled subgraph in `figure_parser.docling_graph` also supports
`graph.stream(inputs, stream_mode="custom")` for per-PDF events. Its final state
contains counts and the output folder, keeping images/text out of graph state.
`ExtractionConfig` exposes resolution (216 DPI by default), layout batch size
(2), and a classification threshold (`0.80` by default). One PDF's page images are retained during
conversion, so memory use grows with that PDF's length; large-volume throughput
has not been benchmarked. Docling's built-in classifier returns only its highest prediction (`top_k=1`).
The accepted classes are `line_chart`, `bar_chart`, `pie_chart`, `scatter_plot`,
`box_plot`, and `table`; other classes are discarded regardless of score. When its score meets the
threshold, the manifest stores one `classification` object with `class_name` and
`confidence`. Layout-detected tables are retained without inventing a classifier score.

Chart/table picture predictions below the cutoff are saved under
`below_treshold/<pdf_name>__<hash_prefix>/<attempt_id>/`, beside the `images`
folder. Their records are in the PDF manifest's `below_threshold` list with
`classification: null`; they do not count as accepted assets. No bounding boxes
are exported, including in captions, related text, and metadata evidence.
Set another cutoff with `--threshold 0.80` or
`config: {"classification_threshold": 0.80}` in the graph input.

Two PDFs and their annotations from the
[PDFFigures2 conference benchmark](https://github.com/allenai/pdffigures2/tree/master/evaluation)
are available locally in `testing_data/pdffigures2/`. `sources.json` records their
download URLs and hashes. These are a small extraction test sample, not a
training corpus. All future downloaded test datasets belong in the project-root
`testing_data/` folder. Generated images and downloaded PDFs are ignored by Git.

```bash
uv run python -m figure_parser.docling_tool testing_data/pdffigures2 --device cuda
LANGSMITH_TEST_TRACKING=false LANGSMITH_TRACING=false uv run pytest tests -q
```

[![CI](https://github.com/langchain-ai/new-langgraph-project/actions/workflows/unit-tests.yml/badge.svg)](https://github.com/langchain-ai/new-langgraph-project/actions/workflows/unit-tests.yml)
[![Integration Tests](https://github.com/langchain-ai/new-langgraph-project/actions/workflows/integration-tests.yml/badge.svg)](https://github.com/langchain-ai/new-langgraph-project/actions/workflows/integration-tests.yml)

This template demonstrates a simple application implemented using [LangGraph](https://github.com/langchain-ai/langgraph), designed for showing how to get started with [LangGraph Server](https://langchain-ai.github.io/langgraph/concepts/langgraph_server/#langgraph-server) and using [LangGraph Studio](https://langchain-ai.github.io/langgraph/concepts/langgraph_studio/), a visual debugging IDE.

<div align="center">
  <img src="./static/studio_ui.png" alt="Graph view in LangGraph studio UI" width="75%" />
</div>

The main agent in `src/agent/graph.py` coordinates worker events and tracks PDF,
VLM, formatting, and storage progress. See [MAIN_AGENT.md](MAIN_AGENT.md) for its
state/event contract and remaining worker adapters. The exported graph currently
stops at the first unwired service hook.

You can extend this graph to orchestrate more complex agentic workflows that can be visualized and debugged in LangGraph Studio.

## Getting Started

1. Install dependencies, along with the [LangGraph CLI](https://langchain-ai.github.io/langgraph/concepts/langgraph_cli/), which will be used to run the server.

```bash
cd path/to/your/app
pip install -e . "langgraph-cli[inmem]"
```

2. (Optional) Customize the code and project as needed. Create a `.env` file if you need to use secrets.

```bash
cp .env.example .env
```

If you want to enable LangSmith tracing, add your LangSmith API key to the `.env` file.

```text
# .env
LANGSMITH_API_KEY=lsv2...
```

3. Start the LangGraph Server.

```shell
langgraph dev
```

For more information on getting started with LangGraph Server, [see here](https://langchain-ai.github.io/langgraph/tutorials/langgraph-platform/local-server/).

## How to customize

1. **Define runtime context**: Modify the `Context` class in the `graph.py` file to expose the arguments you want to configure per assistant. For example, in a chatbot application you may want to define a dynamic system prompt or LLM to use. For more information on runtime context in LangGraph, [see here](https://langchain-ai.github.io/langgraph/agents/context/?h=context#static-runtime-context).

2. **Extend the graph**: The core logic of the application is defined in [graph.py](./src/agent/graph.py). You can modify this file to add new nodes, edges, or change the flow of information.

## Development

While iterating on your graph in LangGraph Studio, you can edit past state and rerun your app from previous states to debug specific nodes. Local changes will be automatically applied via hot reload.

Follow-up requests extend the same thread. You can create an entirely new thread, clearing previous history, using the `+` button in the top right.

For more advanced features and examples, refer to the [LangGraph documentation](https://langchain-ai.github.io/langgraph/). These resources can help you adapt this template for your specific use case and build more sophisticated conversational agents.

LangGraph Studio also integrates with [LangSmith](https://smith.langchain.com/) for more in-depth tracing and collaboration with teammates, allowing you to analyze and optimize your chatbot's performance.
