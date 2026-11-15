import re
from pathlib import Path

import pandas as pd
import pytest

from dgap import models, run
from dgap.models import load_model_config

REPO_ROOT = Path(__file__).resolve().parents[1]
PILOT = str(REPO_ROOT / "data" / "pilot.jsonl")
CONFIG = REPO_ROOT / "config" / "models.yaml"
# The tests run against a placeholder config of two targets, two readers and
# one judge, and the first two pilot scenarios, whatever the repo config holds.
PLACEHOLDER_CONFIG = """\
targets:
  - name: target-1
    model: FILL_ME/target-model-1
  - name: target-2
    model: FILL_ME/target-model-2
readers:
  - name: reader-1
    model: FILL_ME/reader-model-1
  - name: reader-2
    model: FILL_ME/reader-model-2
judges:
  - name: judge-1
    model: FILL_ME/judge-model-1
"""
FILLED = {
    "targets": [{"name": "t", "model": "anthropic/some-model"}],
    "readers": [{"name": "r", "model": "openai/some-model"}],
    "judges": [{"name": "j", "model": "gemini/some-model"}],
}


def call(tmp_path, *extra, run_id="r"):
    config_path = tmp_path / "models.yaml"
    config_path.write_text(PLACEHOLDER_CONFIG, encoding="utf-8")
    argv = ["--scenarios", PILOT, "--run-id", run_id, "--limit", "2", *extra]
    return run.main(argv, config_path=config_path, results_dir=tmp_path / "results")


def dry_run_counts(output):
    return {name: int(count) for name, count in re.findall(r"^\s+(\w+)\s+(\d+)$", output, re.MULTILINE)}


def test_dry_run_prints_exact_counts_and_makes_no_calls(tmp_path, mock_env, monkeypatch, capsys):
    def no_calls(*args, **kwargs):
        raise AssertionError("dry run made a model call")

    monkeypatch.setattr(run.pipeline, "complete", no_calls)
    assert call(tmp_path, "--stage", "all", "--repeats", "2", "--dry-run") == 0
    assert dry_run_counts(capsys.readouterr().out) == {
        "targets": 48, "beliefs": 8, "readers": 48, "judges": 24, "metrics": 0, "total": 128,
    }
    assert not (tmp_path / "results").exists()


def test_dry_run_respects_stage_conditions_and_limit(tmp_path, mock_env, capsys):
    assert call(tmp_path, "--stage", "readers", "--conditions", "neutral,strong", "--limit", "1",
                "--repeats", "3", "--dry-run") == 0
    # 1 scenario x 2 targets x 2 conditions x 3 repeats x 2 readers
    assert dry_run_counts(capsys.readouterr().out) == {"readers": 24, "total": 24}


def test_dry_run_counts_match_records_written(tmp_path, mock_env, capsys):
    assert call(tmp_path, "--repeats", "2", "--dry-run") == 0
    planned = dry_run_counts(capsys.readouterr().out)
    assert call(tmp_path, "--repeats", "2") == 0
    runs = tmp_path / "results" / "raw" / "runs" / "r"
    for stage in ("targets", "beliefs", "readers", "judges"):
        assert len((runs / f"{stage}.jsonl").read_text().splitlines()) == planned[stage]


def test_mock_run_writes_only_mock_named_tables(tmp_path, mock_env, capsys):
    assert call(tmp_path, "--repeats", "2", run_id="pilot") == 0
    tables = sorted(path.name for path in (tmp_path / "results" / "tables").iterdir())
    assert tables == ["pilot_MOCK_items.csv", "pilot_MOCK_summary.csv"]
    summary = pd.read_csv(tmp_path / "results" / "tables" / "pilot_MOCK_summary.csv")
    assert len(summary) == 2 * 3  # targets x conditions
    assert (summary["n"] == 4).all()  # scenarios x repeats
    assert (summary["parse_failure_rate"] == 0).all()
    items = pd.read_csv(tmp_path / "results" / "tables" / "pilot_MOCK_items.csv")
    assert len(items) == 24 and items["mock"].all()


def test_second_run_reports_everything_skipped(tmp_path, mock_env, capsys):
    assert call(tmp_path, "--repeats", "2") == 0
    capsys.readouterr()
    assert call(tmp_path, "--repeats", "2") == 0
    output = capsys.readouterr().out
    assert re.findall(r"new records=(\d+)", output) == ["0", "0", "0", "0"]
    assert re.findall(r"already written=(\d+)", output) == ["48", "8", "48", "24"]


def test_bad_condition_is_rejected(tmp_path, mock_env):
    with pytest.raises(SystemExit):
        call(tmp_path, "--conditions", "neutral,incentive", "--dry-run")


@pytest.mark.parametrize("stage", ["all", "targets", "beliefs", "readers", "judges", "metrics"])
def test_real_run_with_fill_me_fails_before_any_call(tmp_path, real_env, monkeypatch, capsys, stage):
    def no_calls(*args, **kwargs):
        raise AssertionError("a model call was attempted")

    monkeypatch.setattr(run.pipeline, "complete", no_calls)
    assert call(tmp_path, "--stage", stage) == 2
    assert "FILL_ME" in capsys.readouterr().err
    assert not (tmp_path / "results").exists()


def test_placeholder_config_is_rejected(tmp_path):
    path = tmp_path / "models.yaml"
    path.write_text(PLACEHOLDER_CONFIG, encoding="utf-8")
    with pytest.raises(run.ConfigError, match="FILL_ME"):
        run.check_no_placeholders(load_model_config(path))


def test_repo_config_names_a_known_provider_for_every_model():
    config = load_model_config(CONFIG)
    for group in ("targets", "readers", "judges"):
        for entry in config[group]:
            provider = entry["model"].split("/", 1)[0]
            assert provider in run.PROVIDER_KEY_ENV or provider == "FILL_ME"


def test_filled_config_passes_placeholder_check():
    run.check_no_placeholders(FILLED)


def test_missing_api_key_is_named():
    env = {"ANTHROPIC_API_KEY": "x", "GEMINI_API_KEY": "x"}
    with pytest.raises(run.ConfigError, match="OPENAI_API_KEY is not set"):
        run.check_api_keys(FILLED, ["targets", "readers", "judges"], env)
    # Only the groups a stage uses are checked.
    run.check_api_keys(FILLED, ["targets", "judges"], env)


def test_empty_api_key_counts_as_unset():
    with pytest.raises(run.ConfigError, match="ANTHROPIC_API_KEY is not set"):
        run.check_api_keys(FILLED, ["targets"], {"ANTHROPIC_API_KEY": ""})


def test_unknown_provider_is_an_error():
    config = {**FILLED, "targets": [{"name": "t", "model": "some-model-without-prefix"}]}
    with pytest.raises(run.ConfigError, match="cannot tell which API key"):
        run.check_api_keys(config, ["targets"], {})


def test_real_run_with_missing_key_fails_before_any_call(tmp_path, real_env, monkeypatch, capsys):
    monkeypatch.setattr(run, "load_model_config", lambda path: FILLED)
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: False)
    for variable in run.PROVIDER_KEY_ENV.values():
        monkeypatch.delenv(variable, raising=False)
    assert call(tmp_path, "--stage", "targets") == 2
    assert "ANTHROPIC_API_KEY is not set" in capsys.readouterr().err
    assert not (tmp_path / "results").exists()
    assert models._call_litellm.__name__ == "_no_network"
