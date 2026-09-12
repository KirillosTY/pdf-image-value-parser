"""Format an image's saved VLM result and persist progress in its manifest."""

from contextlib import contextmanager
from uuid import uuid4

from parser.src.formatter.model import FormattingError, NuExtractFormatter
from parser.src.redis.state import update_manifest_image
from redis.exceptions import LockNotOwnedError


@contextmanager
def lock_n_update(client, manifest_key: str, image_key: str, schema: dict, model: str):
    """Hold the image lock while formatting and record a unique attempt."""
    lock = client.lock(f"{image_key}:format_lock", timeout=600, blocking=False)
    if not lock.acquire():
        raise RuntimeError("Image is already being formatted")
    try:
        attempt_id = uuid4().hex

        def begin(image):
            if image.get("format_status") == "complete":
                raise ValueError(
                    "Image is already formatted; explicit reformatting is required"
                )
            raw = (image.get("vlm_result") or {}).get("raw_output")
            if (
                image.get("vlm_status") != "complete"
                or not isinstance(raw, str)
                or not raw.strip()
            ):
                raise ValueError("A saved, complete VLM result is required")
            image["format_status"] = "processing"
            image["formatting"] = {
                "attempt_id": attempt_id,
                "model": model,
                "schema": schema,
                "model_response": None,
                "data": None,
                "unmapped_observations": [],
                "error": None,
            }

        image = update_manifest_image(client, manifest_key, image_key, begin)
        yield attempt_id, image["vlm_result"]["raw_output"]
    finally:
        try:
            lock.release()
        except LockNotOwnedError:
            # A lease can expire after a stopped process resumes. The attempt
            # check on each write fences out a worker replaced by a newer one.
            pass


def start_formatting(
    client,
    formatter: NuExtractFormatter,
    *,
    manifest_key: str,
    image_key: str,
    attempt_id: str,
    raw_output: str,
    schema: dict,
) -> dict:
    """Call the model and persist either validated output or the failure."""

    def check_attempt(image):
        if (image.get("formatting") or {}).get("attempt_id") != attempt_id:
            raise RuntimeError("Formatting attempt was superseded")
        if image.get("format_status") != "processing":
            raise RuntimeError("Formatting attempt is no longer processing")

    try:
        result = formatter.format(raw_output, schema)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        response = exc.response if isinstance(exc, FormattingError) else None

        def fail(image):
            check_attempt(image)
            image["format_status"] = "failed"
            image["formatting"]["error"] = error
            image["formatting"]["model_response"] = response

        update_manifest_image(client, manifest_key, image_key, fail)
        raise

    def complete(image):
        check_attempt(image)
        image["formatting"] = {**result, "attempt_id": attempt_id}
        image["format_status"] = "complete"

    return update_manifest_image(client, manifest_key, image_key, complete)


def format_image(
    client,
    formatter: NuExtractFormatter,
    *,
    manifest_key: str,
    image_key: str,
    schema: dict,
) -> dict:
    """Coordinate locking and formatting for a caller-selected schema."""
    with lock_n_update(client, manifest_key, image_key, schema, formatter.model) as (
        attempt_id,
        raw,
    ):
        return start_formatting(
            client,
            formatter,
            manifest_key=manifest_key,
            image_key=image_key,
            attempt_id=attempt_id,
            raw_output=raw,
            schema=schema,
        )
