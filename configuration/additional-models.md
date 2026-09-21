# Additional local models

The user requested two additional VLMs and two additional formatters. The added
VLMs must use less than 4 GB RAM each. The following are small quantized
candidates; **their peak RAM has not been measured, so compliance with that
requirement is not yet established**. No inference tests are authorized for now.

## Configured selection

| Role | Source model | Local alias | Context / maximum output |
| --- | --- | --- | --- |
| VLM | [Qwen3-VL 2B](https://ollama.com/library/qwen3-vl:2b) | `parser-qwen3-vl:2b` | 4,096 / 2,048 |
| VLM | [Granite Vision 2B](https://ollama.com/library/granite3.2-vision:2b) | `parser-granite-vision:2b` | 4,096 / 2,048 |
| Formatter | [Qwen2.5 3B](https://ollama.com/library/qwen2.5:3b) | `parser-qwen-formatter:3b` | 8,192 / 4,096 |
| Formatter | [Llama 3.2 3B](https://ollama.com/library/llama3.2:3b) | `parser-llama-formatter:3b` | 8,192 / 4,096 |

Qwen3-VL provides visual-text/OCR capabilities. Granite Vision specifically
targets document content, including tables, charts and plots. The new formatters
use the existing generic chat adapter. Qwen2.5 supports JSON-oriented tasks;
Llama is an instruction-following alternative whose measurement-formatting
quality remains unverified. Both receive the complete nullable JSON Schema.

The original chart reader, formatter and installed Qwen3 abliterated MainAgent
remain configured. There are now three models available for each worker stage.
`think_sorting=True` enables one shared MainAgent for both routing stages. MainAgent chooses default routes, then makes
per-image/per-result decisions in the visible routing nodes. All configured
candidates have queues when thinking is enabled, so a default route does not
prevent selecting another model for a specific image. Existing runs keep their
stored configuration; the new choices apply to newly created runs.

## Memory and context

Ollama lists approximately 1.9 GB and 2.4 GB packages for the added VLMs. These
are download sizes, not peak RAM figures. Inference also needs image processing,
context caches and server overhead. The aliases restrict context to 4,096 tokens
and the application allows one request per model. This reduces demand but does
not impose a 4 GB memory limit on the shared Ollama server. Estimates remain
`None`; no fabricated RAM/VRAM estimate is used by the scheduler.

The existing 7B reader and MainAgent are outside the requested limit on the
*two added VLMs*. All models share the external Ollama endpoint, which handles
loading, eviction and offload. Simultaneous residency is not guaranteed.
Shorter context/output budgets can truncate complex figures; MainAgent receives
these limitations and can route larger jobs to the original reader/formatter.

## Installation

The four Modelfiles in [models/](models/) set actual server context sizes. A
`context_window` entry in Python alone does not configure Ollama's allocation.
Use Ollama 0.12.7 or later for Qwen3-VL, per its model card.

```bash
ollama pull qwen3-vl:2b
ollama pull granite3.2-vision:2b
ollama pull qwen2.5:3b
ollama pull llama3.2:3b
ollama create parser-qwen3-vl:2b -f configuration/models/qwen3-vl-2b.Modelfile
ollama create parser-granite-vision:2b -f configuration/models/granite-vision-2b.Modelfile
ollama create parser-qwen-formatter:3b -f configuration/models/qwen-formatter-3b.Modelfile
ollama create parser-llama-formatter:3b -f configuration/models/llama-formatter-3b.Modelfile
```

All four downloads and alias creations completed through
`bash configuration/models/install.sh`. All aliases use `OLLAMA_BASE_URL`.
Downloads and alias creation do not evaluate the models; RAM and accuracy checks
remain paused.
