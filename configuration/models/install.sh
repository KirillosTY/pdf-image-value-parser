#!/usr/bin/env bash
# Run from the repository root. Download weights and create aliases; no inference.
set -euo pipefail

ollama pull qwen3-vl:2b
ollama pull granite3.2-vision:2b
ollama pull qwen2.5:3b
ollama pull llama3.2:3b
ollama create parser-qwen3-vl:2b -f configuration/models/qwen3-vl-2b.Modelfile
ollama create parser-granite-vision:2b -f configuration/models/granite-vision-2b.Modelfile
ollama create parser-qwen-formatter:3b -f configuration/models/qwen-formatter-3b.Modelfile
ollama create parser-llama-formatter:3b -f configuration/models/llama-formatter-3b.Modelfile
# Reuse the user's installed MainAgent weights with enough server context.
ollama create parser-main-agent:latest -f configuration/models/main-agent.Modelfile
