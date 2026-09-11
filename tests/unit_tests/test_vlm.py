from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from parser.src.vlm import vlm


def test_extract_chart_preserves_unconstrained_output(monkeypatch):
    raw = "  Panel A: 3.2 ± 0.4 mg.\nUnclear annotation: †\n"
    create = Mock(return_value=SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=raw))]
    ))
    monkeypatch.setattr(vlm, "_client", lambda: SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    ))

    assert vlm.extract_chart(b"image", {"content": "Read all observations"}) == raw
    request = create.call_args.kwargs
    assert "response_format" not in request
    assert request["messages"][0]["content"][1]["image_url"]["url"].startswith(
        "data:image/png;base64,"
    )


@pytest.mark.parametrize("content", [None, "", " \n"])
def test_extract_chart_rejects_missing_output(monkeypatch, content):
    create = Mock(return_value=SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
    ))
    monkeypatch.setattr(vlm, "_client", lambda: SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    ))
    with pytest.raises(ValueError, match="no extraction text"):
        vlm.extract_chart(b"image", {"content": "Read all observations"})
