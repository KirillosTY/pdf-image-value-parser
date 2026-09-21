"""Cover editable profiles and isolation of saved runs from local configuration."""

import importlib
from pathlib import Path

import pytest

from config import PipelineConfig, load_config
from config_files import DEFAULT_RUN_CONFIG


def copy_profile(tmp_path):
    run = tmp_path / "run.toml"
    run.write_text(DEFAULT_RUN_CONFIG.read_text())
    (tmp_path / "models.toml").write_text(DEFAULT_RUN_CONFIG.with_name("models.toml").read_text())
    return run


def test_fresh_loads_see_edits_without_mutating_previous_snapshots(tmp_path):
    path = copy_profile(tmp_path)
    before = load_config(path).snapshot()
    path.write_text(path.read_text().replace("batch_processing = true", "batch_processing = false"))
    after = load_config(path).snapshot()
    assert before["batch_processing"] is True
    assert after["batch_processing"] is False
    assert {**after, "batch_processing": True} == before


def test_model_file_is_relative_to_profile_not_working_directory(tmp_path, monkeypatch):
    profile_dir = tmp_path / "profiles"
    profile_dir.mkdir()
    path = copy_profile(profile_dir)
    monkeypatch.chdir(tmp_path)
    config = load_config(path)
    assert config.main_agent in config.models
    assert config.models[config.main_agent].model_id == "parser-main-agent:latest"


@pytest.mark.parametrize("old,new,expected", [
    ("batch_processing = true", "batch_procesing = true", "Unknown or misplaced"),
    ("batch_processing = true", 'batch_processing = "true"', "must be a boolean"),
    ("recursive = false", 'recursive = "false"', "must be a boolean"),
    ("manifest_capacity = 50", "manifest_capacity = 0", "positive integer"),
])
def test_invalid_profile_edits_are_reported_with_file_name(tmp_path, old, new, expected):
    path = copy_profile(tmp_path)
    path.write_text(path.read_text().replace(old, new))
    with pytest.raises(ValueError, match=expected) as error:
        load_config(path)
    assert str(path) in str(error.value)


def test_empty_main_agent_selection_requires_compatible_routing(tmp_path):
    path = copy_profile(tmp_path)
    models = path.with_name("models.toml")
    models.write_text(models.read_text().replace('main_agent = "main_agent"', 'main_agent = ""'))
    with pytest.raises(ValueError, match="Thinking-based routing requires"):
        load_config(path)
    path.write_text(path.read_text().replace("think_sorting = true", "think_sorting = false"))
    assert load_config(path).main_agent is None


def test_missing_model_file_is_an_explicit_error(tmp_path):
    path = tmp_path / "run.toml"
    path.write_text('models_file = "missing.toml"\n')
    with pytest.raises(ValueError, match="missing.toml"):
        load_config(path)


def test_saved_snapshots_reconstruct_without_reading_toml(monkeypatch):
    snapshot = load_config().snapshot()

    def unavailable(*args, **kwargs):
        raise AssertionError("Loading a snapshot must not read a local file")

    monkeypatch.setattr(Path, "open", unavailable)
    assert PipelineConfig.from_dict(snapshot).snapshot() == snapshot


@pytest.mark.anyio
async def test_existing_run_cli_path_never_loads_local_configuration(monkeypatch):
    cli = importlib.import_module("agent.__main__")

    def unexpected(*args, **kwargs):
        raise AssertionError("An existing run must not load TOML or create a database run")

    class Graph:
        async def ainvoke(self, state, options):
            return state

    monkeypatch.setattr(cli, "load_config", unexpected)
    monkeypatch.setattr(cli, "create_engine", unexpected)
    monkeypatch.setattr(cli, "build_graph", Graph)
    result = await cli.run_pipeline("/pdfs", run_id="existing")
    assert result["run_id"] == "existing"
    with pytest.raises(ValueError, match="saved configuration"):
        await cli.run_pipeline("/pdfs", run_id="existing", config_path="changed.toml")
