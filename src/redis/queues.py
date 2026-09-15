"""Create model queues without resetting existing work."""

import json
from urllib.parse import quote

from redis.exceptions import ResponseError

WORKER_GROUP = "workers"


async def create_model_queues(
    client,
    *,
    run_id: str,
    stage: str,
    model_keys: set[str],
) -> dict[str, str]:
    """Create one stream per model and preserve its consumer group."""
    if not run_id:
        raise ValueError("run_id is required")

    if stage not in {"vlm", "formatter"}:
        raise ValueError("Unsupported queue stage")

    if not model_keys or any(
        not isinstance(key, str) or not key
        for key in model_keys
    ):
        raise ValueError("Provide nonempty model registry keys")

    prefix = f"run_queue:{quote(run_id, safe='')}:{stage}"

    queues = {
        model_key: (
            f"{prefix}:model:{quote(model_key, safe='')}"
        )
        for model_key in sorted(model_keys)
    }

    registry_key = f"{prefix}:registry"
    serialized = json.dumps(queues, sort_keys=True)

    # Record the intended queues once. A retry completes any
    # creation interrupted after this write.
    await client.set(registry_key, serialized, nx=True)

    saved = await client.get(registry_key)
    if saved is None:
        raise RuntimeError("The queue registry disappeared")

    if json.loads(saved) != queues:
        raise ValueError(
            "This run already has a different model queue registry"
        )

    for stream_key in queues.values():
        try:
            await client.xgroup_create(
                name=stream_key,
                groupname=WORKER_GROUP,
                id="0-0",
                mkstream=True,
            )
        except ResponseError as exc:
            # Keep the existing group's progress and pending jobs.
            if not str(exc).startswith("BUSYGROUP"):
                raise

    return queues
