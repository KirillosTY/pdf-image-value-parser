#!/usr/bin/env bash
set -u
log=output/pdf/memory_measurement_app.log
csv=output/pdf/memory_measurement.csv
rm -f "$log" "$csv"
printf 'timestamp_utc,gpu_used_mib,gpu_free_mib,gpu_util_pct,app_rss_mib,ollama_rss_mib,runners_rss_mib\n' > "$csv"
(.venv/bin/python -m agent output/pdf/chart_extraction_memory_input.pdf > "$log" 2>&1) &
app_pid=$!
while kill -0 "$app_pid" 2>/dev/null; do
    timestamp=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    gpu=$(nvidia-smi --query-gpu=memory.used,memory.free,utilization.gpu --format=csv,noheader,nounits 2>/dev/null | head -n 1 | tr -d ' ' || true)
    gpu_used=$(printf '%s' "$gpu" | cut -d, -f1)
    gpu_free=$(printf '%s' "$gpu" | cut -d, -f2)
    gpu_util=$(printf '%s' "$gpu" | cut -d, -f3)
    app_rss=$(ps -o rss= -p "$app_pid" | awk '{printf "%.1f", $1/1024}' 2>/dev/null || printf '0')
    ollama_rss=$(ps -C ollama -o rss= | awk '{sum+=$1} END {printf "%.1f", sum/1024}' 2>/dev/null || printf '0')
    runners_rss=$(ps -C llama-server -o rss= | awk '{sum+=$1} END {printf "%.1f", sum/1024}' 2>/dev/null || printf '0')
    printf '%s,%s,%s,%s,%s,%s,%s\n' "$timestamp" "${gpu_used:-0}" "${gpu_free:-0}" "${gpu_util:-0}" "$app_rss" "$ollama_rss" "$runners_rss" >> "$csv"
    sleep 5
done
wait "$app_pid"
