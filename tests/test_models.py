import json
from pathlib import Path

import pytest

from dgap import models
from dgap.models import complete, load_model_config
from dgap.parse import (
    CONFIRM_LABELS,
    JUDGE_ADVICE_KEYS,
    parse_judge_advice,
    parse_judge_confirm,
    parse_json_object,
    parse_yes_no,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
MESSAGES = [{"role": "user", "content": "hello"}]


def _no_network(*args, **kwargs):
    raise AssertionError("a real provider call was attempted")


@pytest.fixture
def mock_mode(monkeypatch):
    """Mock mode on, and any real provider call fails the test."""
    monkeypatch.setenv("DGAP_MOCK", "1")
    monkeypatch.setattr(models, "_call_litellm", _no_network)


@pytest.fixture
def real_mode(monkeypatch):
    """Mock mode off, with sleeping disabled. Tests must stub _call_litellm."""
    monkeypatch.delenv("DGAP_MOCK", raising=False)
    monkeypatch.setattr(models, "_call_litellm", _no_network)
    monkeypatch.setattr(models.time, "sleep", lambda seconds: None)


def test_mock_returns_expected_shape(mock_mode, tmp_path):
    result = complete("FILL_ME/x", MESSAGES, temperature=0.0, seed=0, cache_dir=tmp_path)
    assert set(result) == {"text", "model", "cached", "usage", "timestamp"}
    assert isinstance(result["text"], str) and result["text"]
    assert result["model"] == "FILL_ME/x"
    assert result["cached"] is False
    assert isinstance(result["usage"], dict)
    assert isinstance(result["timestamp"], str)


def test_second_identical_call_is_cached(mock_mode, tmp_path):
    first = complete("FILL_ME/x", MESSAGES, temperature=0.0, seed=0, cache_dir=tmp_path)
    second = complete("FILL_ME/x", MESSAGES, temperature=0.0, seed=0, cache_dir=tmp_path)
    assert first["cached"] is False
    assert second["cached"] is True
    assert second["text"] == first["text"]
    assert second["timestamp"] == first["timestamp"]


def test_different_seed_is_cache_miss(mock_mode, tmp_path):
    complete("FILL_ME/x", MESSAGES, temperature=0.0, seed=0, cache_dir=tmp_path)
    other = complete("FILL_ME/x", MESSAGES, temperature=0.0, seed=1, cache_dir=tmp_path)
    assert other["cached"] is False


def test_mock_is_deterministic_across_cache_dirs(mock_mode, tmp_path):
    a = complete("FILL_ME/x", MESSAGES, temperature=0.0, seed=0, cache_dir=tmp_path / "a")
    b = complete("FILL_ME/x", MESSAGES, temperature=0.0, seed=0, cache_dir=tmp_path / "b")
    assert a["text"] == b["text"]


def test_mock_cache_is_never_served_to_a_real_run(mock_mode, monkeypatch, tmp_path):
    complete("FILL_ME/x", MESSAGES, temperature=0.0, seed=0, cache_dir=tmp_path)
    monkeypatch.delenv("DGAP_MOCK")
    monkeypatch.setattr(models, "_call_litellm", lambda *a: ("real text", {}))
    result = complete("FILL_ME/x", MESSAGES, temperature=0.0, seed=0, cache_dir=tmp_path)
    assert result["cached"] is False
    assert result["text"] == "real text"


def test_real_cache_hit_makes_no_call(real_mode, monkeypatch, tmp_path):
    calls = []

    def fake(model, messages, temperature, seed):
        calls.append(seed)
        return "real text", {"total_tokens": 3}

    monkeypatch.setattr(models, "_call_litellm", fake)
    first = complete("FILL_ME/x", MESSAGES, temperature=0.7, seed=5, cache_dir=tmp_path)
    second = complete("FILL_ME/x", MESSAGES, temperature=0.7, seed=5, cache_dir=tmp_path)
    assert calls == [5]
    assert first["cached"] is False and second["cached"] is True
    assert second["text"] == "real text"
    assert second["usage"] == {"total_tokens": 3}


def test_retries_then_succeeds(real_mode, monkeypatch, tmp_path):
    attempts = []

    def flaky(model, messages, temperature, seed):
        attempts.append(1)
        if len(attempts) < 3:
            raise RuntimeError("transient")
        return "ok", {}

    monkeypatch.setattr(models, "_call_litellm", flaky)
    result = complete("FILL_ME/x", MESSAGES, temperature=0.0, seed=0, cache_dir=tmp_path)
    assert result["text"] == "ok"
    assert len(attempts) == 3


def test_gives_up_after_four_attempts_and_caches_nothing(real_mode, monkeypatch, tmp_path):
    attempts = []

    def always_fails(model, messages, temperature, seed):
        attempts.append(1)
        raise RuntimeError("down")

    monkeypatch.setattr(models, "_call_litellm", always_fails)
    with pytest.raises(RuntimeError, match="down"):
        complete("FILL_ME/x", MESSAGES, temperature=0.0, seed=0, cache_dir=tmp_path)
    assert len(attempts) == 4
    assert list(tmp_path.rglob("*.json")) == []


def test_model_config_loads():
    config = load_model_config(REPO_ROOT / "config" / "models.yaml")
    assert set(config) == {"targets", "readers", "judges"}
    assert len(config["judges"]) == 1
    for group in ("targets", "readers", "judges"):
        assert config[group]
        for entry in config[group]:
            assert set(entry) == {"name", "model", "rpm", "rpd", "tpd", "max_tokens", "reasoning_effort"}


def test_model_config_rejects_missing_key(tmp_path):
    path = tmp_path / "models.yaml"
    path.write_text("targets:\n  - name: a\nreaders:\n  - name: b\n    model: m\n", encoding="utf-8")
    with pytest.raises(ValueError, match="targets"):
        load_model_config(path)


def test_model_config_requires_judges(tmp_path):
    path = tmp_path / "models.yaml"
    path.write_text(
        "targets:\n  - name: a\n    model: m\nreaders:\n  - name: b\n    model: m\n", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="judges"):
        load_model_config(path)


def _mock(kind, content, tmp_path):
    messages = [{"role": "user", "content": content}]
    return complete("FILL_ME/x", messages, temperature=0.0, seed=0, cache_dir=tmp_path, mock_kind=kind)["text"]


def test_mock_advice_is_short_prose(mock_mode, tmp_path):
    text = _mock("advice", "a", tmp_path)
    assert "MOCK" in text and len(text.split()) > 5
    assert parse_json_object(text, []) is None


def test_mock_confirm_starts_with_yes_or_no(mock_mode, tmp_path):
    answers = {parse_yes_no(_mock("confirm", str(i), tmp_path)) for i in range(20)}
    assert answers == {"YES", "NO"}


def test_mock_probability_is_valid_json(mock_mode, tmp_path):
    values = set()
    for i in range(20):
        obj = json.loads(_mock("probability", str(i), tmp_path))
        assert set(obj) == {"probability", "reason"}
        assert type(obj["probability"]) is int and 0 <= obj["probability"] <= 100
        assert isinstance(obj["reason"], str)
        values.add(obj["probability"])
    assert len(values) > 5


def test_mock_judge_satisfies_both_judge_schemas(mock_mode, tmp_path):
    labels = set()
    for i in range(40):
        text = _mock("judge", str(i), tmp_path)
        assert set(parse_judge_advice(text)) == set(JUDGE_ADVICE_KEYS)
        labels.add(parse_judge_confirm(text))
    assert labels == set(CONFIRM_LABELS)


@pytest.mark.parametrize("kind", ["advice", "confirm", "probability", "judge"])
def test_mock_kind_is_deterministic_and_cached(mock_mode, tmp_path, kind):
    a = _mock(kind, "same", tmp_path / "a")
    b = _mock(kind, "same", tmp_path / "b")
    assert a == b
    messages = [{"role": "user", "content": "same"}]
    again = complete("FILL_ME/x", messages, temperature=0.0, seed=0, cache_dir=tmp_path / "a", mock_kind=kind)
    assert again["cached"] is True and again["text"] == a


def test_unknown_mock_kind_raises(mock_mode, tmp_path):
    with pytest.raises(ValueError, match="mock_kind"):
        _mock("poem", "x", tmp_path)


def test_mock_kind_is_ignored_by_a_real_call(real_mode, monkeypatch, tmp_path):
    monkeypatch.setattr(models, "_call_litellm", lambda *a: ("real text", {}))
    result = complete("FILL_ME/x", MESSAGES, temperature=0.0, seed=0, cache_dir=tmp_path, mock_kind="judge")
    assert result["text"] == "real text"
