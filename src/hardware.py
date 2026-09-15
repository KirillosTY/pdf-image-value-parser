"""Inspect current hardware without importing or loading any model."""

import csv
import json
import os
import platform
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path


def inspect_hardware() -> dict:
    """Report observed resources; leave unavailable measurements unknown."""
    result = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "platform": platform.platform(),
        "cpu": platform.processor() or None,
        "logical_cpu_count": os.cpu_count(),
        "ram_total_gib": None,
        "ram_available_gib": None,
        "gpus": [],
        "notes": [],
    }
    try:
        cpuinfo = Path("/proc/cpuinfo").read_text()
        result["cpu"] = next(
            line.split(":", 1)[1].strip()
            for line in cpuinfo.splitlines()
            if line.startswith("model name")
        )
    except (OSError, StopIteration):
        pass
    try:
        memory = dict(
            line.split(":", 1)
            for line in Path("/proc/meminfo").read_text().splitlines()
        )
        result["ram_total_gib"] = int(memory["MemTotal"].split()[0]) / 1024**2
        result["ram_available_gib"] = int(memory["MemAvailable"].split()[0]) / 1024**2
    except (OSError, KeyError, ValueError):
        result["notes"].append(
            "RAM information unavailable; enter verified measurements during setup."
        )
    if shutil.which("nvidia-smi"):
        try:
            output = subprocess.run(
                [
                    "nvidia-smi",
                    "--query-gpu=uuid,name,memory.total,memory.free",
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True,
                text=True,
                check=True,
                timeout=10,
            ).stdout
            result["gpus"] = [
                {
                    "id": gpu_id.strip(),
                    "name": name.strip(),
                    "backend": "cuda",
                    "vram_total_gib": float(total) / 1024,
                    "vram_available_gib": float(free) / 1024,
                }
                for gpu_id, name, total, free in csv.reader(output.splitlines())
            ]
        except (OSError, ValueError, subprocess.SubprocessError):
            result["notes"].append("nvidia-smi could not report GPU resources.")
    else:
        result["notes"].append(
            "No NVIDIA probe available; other accelerators require manual verification."
        )
    return result


if __name__ == "__main__":
    print(json.dumps(inspect_hardware(), indent=2))  # noqa: T201
