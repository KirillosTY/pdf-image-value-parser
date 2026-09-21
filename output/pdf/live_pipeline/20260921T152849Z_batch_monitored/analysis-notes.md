# Live run analysis (in progress)

Run: `cd939dbb8d1141fda31ef6fd38914e76`; ten chart pages across two PDFs.
The last three pages repeat the first three. Uses the saved batch/think-sorting profile.

- Startup completed in 268 seconds. Eight MainAgent tool decisions; actual tool work took about 0.12 seconds combined.
- Docling completed both documents in 13.23 seconds. Both resource samples during extraction showed zero resident Ollama models.
- MainAgent routed all ten images to `chart_reader` in 153.55 seconds; unused model queues are skipped.
- The first VLM request exceeded the fixed 120-second client timeout; its retry succeeded in 4.96 seconds.
- Ollama explicitly disabled multimodal projector offload with `reason=limited-vram`; its launch command contains `--no-mmproj-offload`, and the vision encoder uses the CPU backend. See `ollama-vlm-load.log` lines 19–20 and 255.
- This establishes a VRAM placement/CPU latency bottleneck in this run. It does not demonstrate a system-RAM out-of-memory failure.
- Residency is sampled every five seconds: samples cannot exclude brief transitions between samples. The runtime also checks residency before stage changes.

While the run was active, `show_thinking` was added and enabled in the editable profile for future runs. This run retains its earlier saved configuration and already imported code.

The live `events.jsonl` contains stage timing and resource evidence; `analysis.json` is regenerated from it. Final database outcomes remain to be checked.
