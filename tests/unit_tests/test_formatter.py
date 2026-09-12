import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import Mock

import fakeredis
import pytest
from jsonschema import Draft202012Validator
from parser.src.formatter.model import (
    FormattingError,
    NuExtractFormatter,
    normalize_fields,
    schema_to_template,
)
from parser.src.formatter.worker import format_image
from parser.src.redis.state import (
    DB_READY,
    load_manifest,
    save_vlm_result,
    update_manifest_image,
)

from schema_format import BAR_CHART, CHART_SCHEMAS, PIE_CHART


def manifest_for(key="pdf:1", count=2):
    return {
        "manifest_key": key,
        "document_id": key,
        "source_path": "/papers/test.pdf",
        "status": "completed",
        "format_status": "not_started",
        "db_status": "not_started",
        "document_metadata": {"title": "Test", "publication_year": 2024},
        "assets": [
            {
                "asset_id": "asset",
                "name": "Figure 1",
                "images_meta": [
                    {
                        "redis_key": f"{key}:image:{i}",
                        "manifest_key": key,
                        "page_number": 1,
                        "width": 100,
                        "height": 80,
                        "vlm_status": "not_started",
                        "vlm_result": None,
                        "format_status": "not_started",
                        "formatting": None,
                    }
                    for i in range(count)
                ],
            }
        ],
    }


def model_response(data=None, **kwargs):
    data = data or {
        "chart_type": "BAR_CHART",
        "data": [{"category": "2001", "series": "Control", "value": 12.4}],
    }
    return json.dumps(
        {"data": data, "unmapped_observations": ["Error bar ±0.8"], **kwargs}
    )


def formatter_for(text=None, finish_reason="stop"):
    create = Mock(
        return_value=SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason=finish_reason,
                    message=SimpleNamespace(
                        content=model_response() if text is None else text
                    ),
                )
            ]
        )
    )
    return NuExtractFormatter(
        SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create))
        )
    ), create


@pytest.fixture
def client():
    return fakeredis.FakeRedis()


def test_every_existing_schema_has_a_template():
    for schema in CHART_SCHEMAS.values():
        template = schema_to_template(schema)
        assert template["chart_type"] == [schema["properties"]["chart_type"]["const"]]
    assert schema_to_template(PIE_CHART)["donut"] == ["true", "false"]


def test_raw_text_and_unmatched_observation_survive():
    formatter, create = formatter_for()
    raw = "  Control in 2001: 12.4; error bar ±0.8.\n"
    result = formatter.format(raw, BAR_CHART)
    assert result["data"]["data"][0]["value"] == 12.4
    assert result["unmapped_observations"] == ["Error bar ±0.8"]
    assert result["model_response"] == model_response()
    request = create.call_args.kwargs
    assert request["messages"][0]["content"] == [{"type": "text", "text": raw}]
    assert "response_format" not in request
    assert json.loads(request["extra_body"]["chat_template_kwargs"]["template"])[
        "data"
    ]["chart_type"] == ["BAR_CHART"]


@pytest.mark.parametrize(
    "text,reason",
    [
        ("not json", "stop"),
        (model_response(), "length"),
        ('{"data":{},"data":{},"unmapped_observations":[]}', "stop"),
        (
            model_response(
                {
                    "chart_type": "BAR_CHART",
                    "data": [{"category": "A", "series": "S", "value": None}],
                }
            ),
            "stop",
        ),
        (model_response().replace("12.4", "NaN"), "stop"),
        (model_response().replace("12.4", "1e999"), "stop"),
        ("[]", "stop"),
    ],
)
def test_invalid_response_is_retained(text, reason):
    formatter, _ = formatter_for(text, reason)
    with pytest.raises(FormattingError) as error:
        formatter.format("observations", BAR_CHART)
    assert error.value.response == text


def test_normalization_recurses_without_inventing_measurements():
    value = {
        "chart_type": "PIE_CHART",
        "title": None,
        "donut": "false",
        "data": [{"label": "A", "value": None}],
    }
    result = normalize_fields(value, PIE_CHART)
    assert result["donut"] is False
    assert "title" not in result
    assert result["data"][0]["value"] is None
    assert not Draft202012Validator(PIE_CHART).is_valid(result)


def test_document_ready_only_after_every_image(client):
    manifest = manifest_for()
    client.set("pdf:1", json.dumps(manifest))
    formatter, _ = formatter_for()
    for i in range(2):
        image_key = f"pdf:1:image:{i}"
        save_vlm_result(client, "pdf:1", image_key, "original text", model="vlm")
        format_image(
            client,
            formatter,
            manifest_key="pdf:1",
            image_key=image_key,
            schema=BAR_CHART,
        )
        saved = load_manifest(client, "pdf:1")
        assert saved["format_status"] == ("processing" if i == 0 else "complete")
        assert client.zcard(DB_READY) == i
    image = saved["assets"][0]["images_meta"][0]
    assert image["vlm_result"]["raw_output"] == "original text"
    assert image["vlm_status"] == "complete"
    assert "db_status" not in image
    assert saved["db_status"] == "not_started"


def test_failure_is_saved_and_document_never_becomes_ready(client):
    client.set("pdf:1", json.dumps(manifest_for(count=1)))
    formatter, _ = formatter_for("invalid output")
    save_vlm_result(client, "pdf:1", "pdf:1:image:0", "original")
    with pytest.raises(FormattingError):
        format_image(
            client,
            formatter,
            manifest_key="pdf:1",
            image_key="pdf:1:image:0",
            schema=BAR_CHART,
        )
    saved = load_manifest(client, "pdf:1")
    image = saved["assets"][0]["images_meta"][0]
    assert image["format_status"] == saved["format_status"] == "failed"
    assert image["formatting"]["model_response"] == "invalid output"
    assert image["vlm_result"]["raw_output"] == "original"
    assert client.zcard(DB_READY) == 0
    formatter, _ = formatter_for()
    format_image(
        client,
        formatter,
        manifest_key="pdf:1",
        image_key="pdf:1:image:0",
        schema=BAR_CHART,
    )
    assert client.zcard(DB_READY) == 1


def test_worker_requires_saved_vlm_output(client):
    client.set("pdf:1", json.dumps(manifest_for()))
    formatter, create = formatter_for()
    with pytest.raises(ValueError, match="saved, complete VLM"):
        format_image(
            client,
            formatter,
            manifest_key="pdf:1",
            image_key="pdf:1:image:0",
            schema=BAR_CHART,
        )
    create.assert_not_called()
    assert load_manifest(client, "pdf:1")["format_status"] == "not_started"


def test_concurrent_updates_preserve_both_images(client):
    client.set("pdf:1", json.dumps(manifest_for()))
    barrier = Barrier(2)

    def work(i):
        first = True

        def update(image):
            nonlocal first
            if first:
                first = False
                barrier.wait(timeout=5)
            image["vlm_status"] = "processing"

        update_manifest_image(client, "pdf:1", f"pdf:1:image:{i}", update)

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(work, range(2)))
    assert all(
        image["vlm_status"] == "processing"
        for image in load_manifest(client, "pdf:1")["assets"][0]["images_meta"]
    )


def test_superseded_attempt_cannot_publish(client):
    client.set("pdf:1", json.dumps(manifest_for(count=1)))
    save_vlm_result(client, "pdf:1", "pdf:1:image:0", "original")
    formatter, _ = formatter_for()
    original_format = formatter.format

    def supersede(raw, schema):
        update_manifest_image(
            client,
            "pdf:1",
            "pdf:1:image:0",
            lambda image: image["formatting"].update(attempt_id="newer"),
        )
        return original_format(raw, schema)

    formatter.format = supersede
    with pytest.raises(RuntimeError, match="superseded"):
        format_image(
            client,
            formatter,
            manifest_key="pdf:1",
            image_key="pdf:1:image:0",
            schema=BAR_CHART,
        )
    assert client.zcard(DB_READY) == 0


def test_vlm_result_and_formatting_job_commit_once(client):
    from parser.src.redis.state import FORMAT_READY

    client.set("pdf:1", json.dumps(manifest_for(count=1)))
    for _ in range(2):
        image = save_vlm_result(client, "pdf:1", "pdf:1:image:0", "raw text")
    assert image["vlm_status"] == "complete"
    assert image["vlm_result"]["format_job_enqueued"] is True
    jobs = client.xrange(FORMAT_READY)
    assert len(jobs) == 1
    assert jobs[0][1] == {b"manifest_key": b"pdf:1", b"image_key": b"pdf:1:image:0"}


def test_bad_queue_type_leaves_vlm_result_unmodified(client):
    from parser.src.redis.state import FORMAT_READY

    original = json.dumps(manifest_for(count=1))
    client.set("pdf:1", original)
    client.set(FORMAT_READY, "wrong type")
    with pytest.raises(ValueError, match="must be a Redis stream"):
        save_vlm_result(client, "pdf:1", "pdf:1:image:0", "raw text")
    assert client.get("pdf:1").decode() == original


def test_retry_after_lost_transaction_reply_does_not_duplicate_job(client, monkeypatch):
    from parser.src.redis.state import FORMAT_READY
    from redis.exceptions import ConnectionError

    client.set("pdf:1", json.dumps(manifest_for(count=1)))
    pipeline = client.pipeline

    def lost_reply_pipeline(*args, **kwargs):
        pipe = pipeline(*args, **kwargs)
        execute = pipe.execute

        def lost_reply(*args, **kwargs):
            execute(*args, **kwargs)
            raise ConnectionError("Commit succeeded but reply was lost")

        pipe.execute = lost_reply
        return pipe

    with monkeypatch.context() as patch:
        patch.setattr(client, "pipeline", lost_reply_pipeline)
        with pytest.raises(ConnectionError):
            save_vlm_result(client, "pdf:1", "pdf:1:image:0", "raw text")
    save_vlm_result(client, "pdf:1", "pdf:1:image:0", "raw text")
    assert client.xlen(FORMAT_READY) == 1
    formatter, _ = formatter_for()
    format_image(
        client,
        formatter,
        manifest_key="pdf:1",
        image_key="pdf:1:image:0",
        schema=BAR_CHART,
    )
    # An upstream retry is also harmless after formatting has already finished.
    save_vlm_result(client, "pdf:1", "pdf:1:image:0", "raw text")
    assert client.xlen(FORMAT_READY) == 1
