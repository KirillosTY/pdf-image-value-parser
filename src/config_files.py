"""Read user-editable TOML settings without contacting services or changing snapshots."""

from __future__ import annotations

import sys
from dataclasses import fields
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

from config import ModelConfig, PipelineConfig, RedisRecoveryConfig

DEFAULT_RUN_CONFIG = Path(__file__).resolve().parents[1] / "configuration" / "run.toml"
MODEL_FILE_FIELDS = {
    "models", "hardware", "main_agent", "fallback_vlm", "fallback_formatter",
    "chart_matcher", "format_matcher", "memory_reserve_gib",
}
RUN_FILE_FIELDS = {item.name for item in fields(PipelineConfig)} - MODEL_FILE_FIELDS


def read_toml(path: Path) -> dict:
    """Name the offending file when a required configuration is missing or malformed."""
    try:
        with path.open("rb") as source:
            return tomllib.load(source)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"Cannot read configuration {path}: {exc}") from exc


def check_keys(values, allowed, location):
    """Reject misspelled or misplaced settings instead of silently ignoring them."""
    if not isinstance(values, dict):
        raise ValueError(f"{location} must be a TOML table")
    unknown = set(values) - allowed
    if unknown:
        raise ValueError(f"Unknown or misplaced settings in {location}: {', '.join(sorted(unknown))}")


def load_config(run_path: str | Path | None = None) -> PipelineConfig:
    """Load fresh settings for a new run, keeping existing run snapshots independent."""
    path = Path(run_path).expanduser().resolve() if run_path is not None else DEFAULT_RUN_CONFIG
    run = read_toml(path)
    model_file = run.pop("models_file", "models.toml")
    if not isinstance(model_file, str) or not model_file.strip():
        raise ValueError(f"{path}: models_file must be a nonempty path")
    model_path = Path(model_file).expanduser()
    if not model_path.is_absolute():
        model_path = path.parent / model_path
    models = read_toml(model_path)
    check_keys(run, RUN_FILE_FIELDS, path)
    check_keys(models, MODEL_FILE_FIELDS, model_path)
    registry = models.get("models", {})
    if not isinstance(registry, dict):
        raise ValueError(f"{model_path}: models must be a table of model definitions")
    for key, model in registry.items():
        check_keys(model, {item.name for item in fields(ModelConfig)}, f"{model_path} [models.{key}]")
    check_keys(run.get("redis_recovery", {}), {item.name for item in fields(RedisRecoveryConfig)},
               f"{path} [redis_recovery]")
    # TOML has no null. An empty selection explicitly opts out of the MainAgent.
    if models.get("main_agent") == "":
        models["main_agent"] = None
    try:
        config = PipelineConfig.from_dict({**run, **models})
        config.validate()
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError(f"Invalid configuration in {path} / {model_path}: {exc}") from exc
    return config
