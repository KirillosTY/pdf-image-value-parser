from copy import deepcopy

import pytest

from config import ModelConfig, PipelineConfig


def configured():
    return PipelineConfig(
        models={
            "vision": ModelConfig(
                model_id="test-vision",
                roles=["vlm"],
                context="Test fixture.",
                input_description="PNG and caption",
                output_description="Unrestricted observations",
            ),
            "format": ModelConfig(
                model_id="test-format",
                roles=["formatter"],
                context="Test fixture.",
                input_description="Observations and schema",
                output_description="Validated JSON",
            ),
        },
        charts_targeted=["bar_chart"],
        fallback_schema="BAR_CHART",
    )


def test_thinking_is_disabled_independently_for_single_model_stages():
    config = configured()
    config.vlm_think_sorting = config.formatter_think_sorting = True
    config.models["second-format"] = deepcopy(config.models["format"])
    config.models["agent"] = ModelConfig(
        model_id="test-agent",
        roles=["main_agent"],
        context="Select a formatter.",
        input_description="Observations",
        output_description="A formatter identifier",
    )
    config.main_agent = "agent"
    snapshot = config.snapshot(["BAR_CHART"])
    assert snapshot["vlm_think_sorting"] is False
    assert snapshot["formatter_think_sorting"] is True
    assert snapshot["fallback_vlm"] == "vision"
    assert PipelineConfig.from_dict(snapshot).snapshot(["BAR_CHART"]) == snapshot


def test_multiple_models_require_full_mapping_and_unknown_fallback():
    config = configured()
    config.models["second-vision"] = deepcopy(config.models["vision"])
    config.chart_matcher = {key: "vision" for key in config.docling_charts}
    with pytest.raises(ValueError, match="fallback_vlm"):
        config.validate()
    config.fallback_vlm = "second-vision"
    config.validate()
    config.chart_matcher["bar_chart"] = "missing"
    with pytest.raises(ValueError, match="unavailable model"):
        config.validate()


@pytest.mark.parametrize(
    "field,value",
    [
        ("approve_with_fails", True),
        ("approve_with_fails", "skip"),
        ("max_image_retries", -1),
        ("max_image_retries", True),
        ("fallback_schema", "MISSING"),
    ],
)
def test_invalid_policy_or_schema_is_rejected(field, value):
    config = configured()
    setattr(config, field, value)
    with pytest.raises(ValueError):
        config.snapshot(["BAR_CHART"])


def test_unknown_resources_remain_unknown_and_omit_is_preserved():
    config = configured()
    config.approve_with_fails = "omit"
    snapshot = config.snapshot(["BAR_CHART"])
    assert snapshot["approve_with_fails"] == "omit"
    assert snapshot["models"]["vision"]["estimated_vram_gib"] is None
    config.models["vision"].estimated_vram_gib = float("nan")
    with pytest.raises(ValueError):
        config.snapshot(["BAR_CHART"])
