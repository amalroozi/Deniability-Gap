"""Free-tier safety: pacing, 429 handling, the daily budget guard, output caps
and reasoning stripping. Nothing here touches the network."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

from dgap import models, pipeline, run
from dgap.models import complete, load_model_config, strip_reasoning
from dgap.pipeline import CONDITIONS, Budget, BudgetExceeded, daily_usage, read_jsonl, usage_tokens
from dgap.schema import load_scenarios

REPO_ROOT = Path(__file__).resolve().parents[1]
PILOT = REPO_ROOT / "data" / "pilot.jsonl"
SCENARIOS = load_scenarios(PILOT)[:2]
MESSAGES = [{"role": "user", "content": "hello"}]


class FakeClock:
    """Stands in for time.monotonic and time.sleep; sleeping advances the clock."""

    def __init__(self):
        self.now = 1000.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class RateLimitError(Exception):
    """Same class name as litellm's HTTP 429 error."""

    status_code = 429

    def __init__(self, message="rate limited", headers=None):
        super().__init__(message)
        if headers is not None:
            self.response = type("Response", (), {"headers": headers})()


@pytest.fixture
def clock(monkeypatch):
    """Mock mode off, a fake clock, and fresh pacing and 429 state."""
    fake = FakeClock()
    monkeypatch.delenv("DGAP_MOCK", raising=False)
    monkeypatch.setattr(models.time, "monotonic", fake.monotonic)
    monkeypatch.setattr(models.time, "sleep", fake.sleep)
    monkeypatch.setattr(models, "_last_call", {})
    monkeypatch.setattr(models, "rate_limit_events", [])
    return fake


def stub(monkeypatch, function):
    monkeypatch.setattr(models, "_call_litellm", function)


# ----------------------------------------------------------------- pacing


def test_calls_to_one_model_are_spaced_by_its_rpm(clock, monkeypatch, tmp_path):
    times = []

    def fake(model, messages, temperature, seed, **options):
        times.append(clock.now)
        return "ok", {}

    stub(monkeypatch, fake)
    for seed in range(4):
        complete("groq/m", MESSAGES, temperature=0.0, seed=seed, cache_dir=tmp_path, rpm=30)
    gaps = [b - a for a, b in zip(times, times[1:])]
    assert len(gaps) == 3
    assert all(gap >= 60 / 30 for gap in gaps)  # never faster than 30 a minute
    assert all(gap <= 60 / 30 * 1.1 for gap in gaps)  # and not needlessly slow


def test_pacing_is_per_model_and_skips_when_enough_time_has_passed(clock, monkeypatch, tmp_path):
    stub(monkeypatch, lambda *a, **k: ("ok", {}))
    complete("groq/a", MESSAGES, temperature=0.0, seed=0, cache_dir=tmp_path, rpm=30)
    complete("groq/b", MESSAGES, temperature=0.0, seed=0, cache_dir=tmp_path, rpm=30)
    assert clock.sleeps == []  # a different model has its own clock
    clock.now += 10
    complete("groq/a", MESSAGES, temperature=0.0, seed=1, cache_dir=tmp_path, rpm=30)
    assert clock.sleeps == []


def test_no_rpm_means_no_pacing(clock, monkeypatch, tmp_path):
    stub(monkeypatch, lambda *a, **k: ("ok", {}))
    for seed in range(3):
        complete("groq/m", MESSAGES, temperature=0.0, seed=seed, cache_dir=tmp_path)
    assert clock.sleeps == []


def test_cache_hits_and_mock_calls_are_not_paced(clock, monkeypatch, tmp_path):
    stub(monkeypatch, lambda *a, **k: ("ok", {}))
    complete("groq/m", MESSAGES, temperature=0.0, seed=0, cache_dir=tmp_path, rpm=1)
    again = complete("groq/m", MESSAGES, temperature=0.0, seed=0, cache_dir=tmp_path, rpm=1)
    assert again["cached"] is True and clock.sleeps == []
    monkeypatch.setenv("DGAP_MOCK", "1")
    for seed in range(3):
        complete("groq/m", MESSAGES, temperature=0.0, seed=seed, cache_dir=tmp_path, rpm=1)
    assert clock.sleeps == []


def test_retries_are_paced_too(clock, monkeypatch, tmp_path):
    times = []

    def flaky(model, messages, temperature, seed, **options):
        times.append(clock.now)
        if len(times) < 3:
            raise RateLimitError(headers={"retry-after": "0"})
        return "ok", {}

    stub(monkeypatch, flaky)
    complete("gemini/m", MESSAGES, temperature=0.0, seed=0, cache_dir=tmp_path, rpm=15)
    assert all(b - a >= 60 / 15 for a, b in zip(times, times[1:]))


def test_pipeline_passes_each_models_rpm(clock, monkeypatch, tmp_path):
    times = {}

    def fake(model, messages, temperature, seed, **options):
        times.setdefault(model, []).append(clock.now)
        return "NO. Fine.", {}

    stub(monkeypatch, fake)
    targets = [{"name": "a", "model": "groq/a", "rpm": 30}, {"name": "b", "model": "gemini/b", "rpm": 15}]
    pipeline.run_targets("r", SCENARIOS[:1], targets, CONDITIONS, 1, runs_dir=tmp_path / "runs",
                         cache_dir=tmp_path / "cache")
    for model, rpm in (("groq/a", 30), ("gemini/b", 15)):
        assert len(times[model]) == 6
        assert all(b - a >= 60 / rpm for a, b in zip(times[model], times[model][1:]))


# -------------------------------------------------------------- HTTP 429


def test_429_honours_retry_after_header(clock, monkeypatch, tmp_path):
    attempts = []

    def limited(model, messages, temperature, seed, **options):
        attempts.append(1)
        if len(attempts) == 1:
            raise RateLimitError(headers={"retry-after": "7"})
        return "ok", {}

    stub(monkeypatch, limited)
    result = complete("groq/m", MESSAGES, temperature=0.0, seed=0, cache_dir=tmp_path)
    assert result["text"] == "ok" and len(attempts) == 2
    assert clock.sleeps == [7.0]
    assert models.rate_limit_events == [{"model": "groq/m", "attempt": 1, "wait": 7.0}]


@pytest.mark.parametrize(
    "message, expected",
    [
        ("Rate limit reached. Please try again in 7.5s.", 7.5),
        ("Rate limit reached. Please try again in 1m30s.", 90.0),
        ('quota exceeded ... "retryDelay": "23s"', 23.0),
    ],
)
def test_429_delay_is_read_from_the_error_message(message, expected):
    assert models.retry_after_seconds(RateLimitError(message)) == expected


def test_429_without_retry_after_backs_off_up_to_60s_for_six_attempts(clock, monkeypatch, tmp_path):
    attempts = []

    def always_limited(model, messages, temperature, seed, **options):
        attempts.append(1)
        raise RateLimitError()

    stub(monkeypatch, always_limited)
    with pytest.raises(RateLimitError):
        complete("groq/m", MESSAGES, temperature=0.0, seed=0, cache_dir=tmp_path)
    assert len(attempts) == 6
    assert clock.sleeps == [5.0, 10.0, 20.0, 40.0, 60.0]
    assert max(clock.sleeps) <= 60
    assert list(tmp_path.rglob("*.json")) == []  # a failed call is never cached
    assert len(models.rate_limit_events) == 6


def test_429_asking_for_a_very_long_wait_fails_at_once(clock, monkeypatch, tmp_path):
    attempts = []

    def out_of_quota(model, messages, temperature, seed, **options):
        attempts.append(1)
        raise RateLimitError(headers={"retry-after": "3600"})

    stub(monkeypatch, out_of_quota)
    with pytest.raises(RateLimitError):
        complete("groq/m", MESSAGES, temperature=0.0, seed=0, cache_dir=tmp_path)
    assert len(attempts) == 1 and clock.sleeps == []


def test_429s_do_not_use_up_the_attempts_for_other_errors(clock, monkeypatch, tmp_path):
    outcomes = [RateLimitError(headers={"retry-after": "1"})] * 4 + [RuntimeError("blip")] * 3

    def fake(model, messages, temperature, seed, **options):
        if outcomes:
            raise outcomes.pop(0)
        return "ok", {}

    stub(monkeypatch, fake)
    assert complete("groq/m", MESSAGES, temperature=0.0, seed=0, cache_dir=tmp_path)["text"] == "ok"


def test_persistent_429_writes_no_record_and_is_not_a_parse_failure(clock, monkeypatch, tmp_path):
    def always_limited(model, messages, temperature, seed, **options):
        raise RateLimitError()

    stub(monkeypatch, always_limited)
    dirs = {"runs_dir": tmp_path / "runs", "cache_dir": tmp_path / "cache"}
    targets = [{"name": "t", "model": "groq/t"}]
    with pytest.raises(RateLimitError):
        pipeline.run_targets("r", SCENARIOS, targets, CONDITIONS, 1, **dirs)
    assert read_jsonl(tmp_path / "runs" / "r" / "targets.jsonl") == []
    assert list((tmp_path / "cache").rglob("*.json")) == []


def test_run_reports_a_persistent_error_and_can_resume(clock, monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("GROQ_API_KEY", "sk-secret-value")
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: False)
    config = tmp_path / "models.yaml"
    config.write_text(
        "targets:\n  - {name: t, model: groq/t}\nreaders:\n  - {name: r, model: groq/r}\n"
        "judges:\n  - {name: j, model: groq/j}\n",
        encoding="utf-8",
    )
    state = {"fail": True, "calls": 0}

    def fake(model, messages, temperature, seed, **options):
        state["calls"] += 1
        if state["fail"] and state["calls"] > 3:
            raise RateLimitError("limit hit for key sk-secret-value", headers={"retry-after": "9999"})
        text = messages[-1]["content"]
        if "confirm_label" in text:
            return json.dumps({"states_p_explicitly": False, "contradicts_p_explicitly": True,
                               "discloses_adverse_fact": True, "confirm_label": "DENY"}), {}
        if "probability" in text:
            return '{"probability": 20, "reason": "x"}', {}
        return "NO. It is not.", {}

    stub(monkeypatch, fake)
    argv = ["--scenarios", str(PILOT), "--run-id", "r", "--limit", "1"]
    kwargs = {"config_path": config, "results_dir": tmp_path / "results"}
    assert run.main(argv, **kwargs) == 1
    captured = capsys.readouterr()
    assert "stage targets failed" in captured.err and "RateLimitError" in captured.err
    assert "sk-secret-value" not in captured.err + captured.out
    targets_file = tmp_path / "results" / "raw" / "runs" / "r" / "targets.jsonl"
    assert len(read_jsonl(targets_file)) == 3
    assert not (tmp_path / "results" / "tables").exists()
    # Once the provider recovers, the same command finishes the run.
    state["fail"] = False
    assert run.main(argv, **kwargs) == 0
    assert len(read_jsonl(targets_file)) == 6
    assert (tmp_path / "results" / "tables" / "r_summary.csv").is_file()


# ---------------------------------------------------------- budget guard


def _record(model="groq/m", *, tokens=100, hours_ago=1.0, cached=False, mock=False, usage=None):
    when = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    return {
        "model_string": model,
        "usage": usage if usage is not None else {"total_tokens": tokens},
        "cached": cached,
        "mock": mock,
        "timestamp": when.isoformat(),
    }


def _write_run(runs_dir, run_id, stage, records):
    path = runs_dir / run_id / f"{stage}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")


def test_usage_tokens_includes_reasoning():
    assert usage_tokens({"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}) == 150
    # Reasoning reported inside completion_tokens but left out of total_tokens.
    assert usage_tokens({"prompt_tokens": 100, "completion_tokens": 250, "total_tokens": 150,
                         "reasoning_tokens": 200}) == 350
    assert usage_tokens({"prompt_tokens": 10, "completion_tokens": 5}) == 15
    assert usage_tokens({}) == 0 and usage_tokens(None) == 0


def test_daily_usage_counts_recent_real_uncached_records_from_every_run(tmp_path):
    _write_run(tmp_path, "run-a", "targets", [_record(tokens=100), _record(tokens=50)])
    _write_run(tmp_path, "run-b", "judges", [
        _record(tokens=7),
        _record(tokens=1000, hours_ago=25),  # older than 24 hours
        _record(tokens=1000, cached=True),  # made no request
        _record(tokens=1000, mock=True),  # not a real call
        _record("gemini/x", tokens=5),
    ])
    assert daily_usage(tmp_path, mock=False) == {
        "groq/m": {"requests": 3, "tokens": 157},
        "gemini/x": {"requests": 1, "tokens": 5},
    }
    assert daily_usage(tmp_path / "missing", mock=False) == {}


def test_budget_refuses_the_call_that_would_pass_90_percent_of_rpd(tmp_path):
    _write_run(tmp_path, "old", "targets", [_record() for _ in range(8)])
    budget = Budget(tmp_path, mock=False)
    entry = {"name": "m", "model": "groq/m", "rpd": 10, "tpd": None}
    budget.check(entry, MESSAGES, 300)  # the 9th request is exactly 90%
    budget.add(entry, {"total_tokens": 1})
    with pytest.raises(BudgetExceeded, match="request budget.*rpd=10"):
        budget.check(entry, MESSAGES, 300)
    budget.check({"name": "other", "model": "groq/other", "rpd": 10}, MESSAGES, 300)
    budget.check({"name": "m", "model": "groq/m"}, MESSAGES, 300)  # no caps set


def test_budget_refuses_the_call_that_would_pass_90_percent_of_tpd(tmp_path):
    _write_run(tmp_path, "old", "targets", [_record(tokens=8000)])
    budget = Budget(tmp_path, mock=False)
    entry = {"name": "m", "model": "groq/m", "rpd": None, "tpd": 10000}
    budget.check(entry, MESSAGES, 300)
    with pytest.raises(BudgetExceeded, match="token budget.*tpd=10000"):
        budget.check(entry, MESSAGES, 1500)
    budget.add(entry, {"prompt_tokens": 400, "completion_tokens": 700})
    with pytest.raises(BudgetExceeded):
        budget.check(entry, MESSAGES, 300)


def test_stage_stops_cleanly_on_rpd_and_resumes(mock_env, tmp_path):
    dirs = {"runs_dir": tmp_path / "runs", "cache_dir": tmp_path / "cache"}
    path = tmp_path / "runs" / "r" / "targets.jsonl"
    capped = [{"name": "t", "model": "FILL_ME/t", "rpd": 10}]
    stats = pipeline.run_targets("r", SCENARIOS, capped, CONDITIONS, 1, **dirs)
    assert stats["written"] == 9  # 90% of rpd=10
    assert "daily request budget" in stats["stopped"]
    assert len(read_jsonl(path)) == 9
    # Still over budget: nothing new is written and nothing is lost.
    stats = pipeline.run_targets("r", SCENARIOS, capped, CONDITIONS, 1, **dirs)
    assert stats["written"] == 0 and stats["skipped"] == 9 and stats["stopped"]
    # With headroom the same command finishes the stage, with no duplicates.
    roomy = [{"name": "t", "model": "FILL_ME/t", "rpd": 1000}]
    stats = pipeline.run_targets("r", SCENARIOS, roomy, CONDITIONS, 1, **dirs)
    assert stats["stopped"] is None and stats["written"] == 3 and stats["skipped"] == 9
    keys = [pipeline.record_key(record) for record in read_jsonl(path)]
    assert len(keys) == len(set(keys)) == 12


def test_budget_counts_requests_made_under_other_run_ids(mock_env, tmp_path):
    dirs = {"runs_dir": tmp_path / "runs", "cache_dir": tmp_path / "cache"}
    capped = [{"name": "t", "model": "FILL_ME/t", "rpd": 10}]
    pipeline.run_beliefs("first", SCENARIOS, capped, 4, **dirs)  # 8 requests
    stats = pipeline.run_targets("second", SCENARIOS, capped, CONDITIONS, 1, **dirs)
    assert stats["written"] == 1 and stats["stopped"]


def test_cache_hits_do_not_use_budget(mock_env, tmp_path):
    cache = tmp_path / "cache"
    capped = [{"name": "t", "model": "FILL_ME/t", "rpd": 20}]
    first = pipeline.run_targets("a", SCENARIOS, capped, CONDITIONS, 1, runs_dir=tmp_path / "runs1", cache_dir=cache)
    assert first["written"] == 12 and first["stopped"] is None
    # Same calls under a new run id are all served from the cache.
    again = pipeline.run_targets("b", SCENARIOS, capped, CONDITIONS, 1, runs_dir=tmp_path / "runs1", cache_dir=cache)
    assert again["written"] == 12 and again["cached"] == 12 and again["stopped"] is None


def test_token_budget_stops_a_stage_using_recorded_usage(clock, monkeypatch, tmp_path):
    stub(monkeypatch, lambda *a, **k: ("NO. Fine.", {"prompt_tokens": 500, "completion_tokens": 500,
                                                       "total_tokens": 1000}))
    dirs = {"runs_dir": tmp_path / "runs", "cache_dir": tmp_path / "cache"}
    capped = [{"name": "t", "model": "groq/t", "tpd": 10000}]
    stats = pipeline.run_targets("r", SCENARIOS, capped, CONDITIONS, 1, **dirs)
    assert "daily token budget" in stats["stopped"]
    written = read_jsonl(tmp_path / "runs" / "r" / "targets.jsonl")
    assert 0 < len(written) < 12
    assert sum(usage_tokens(record["usage"]) for record in written) <= 0.9 * 10000


def test_run_stops_at_the_budget_and_skips_later_stages(mock_env, tmp_path, capsys):
    config = tmp_path / "models.yaml"
    config.write_text(
        "targets:\n  - {name: t, model: FILL_ME/t, rpd: 10}\nreaders:\n  - {name: r, model: FILL_ME/r}\n"
        "judges:\n  - {name: j, model: FILL_ME/j}\n",
        encoding="utf-8",
    )
    argv = ["--scenarios", str(PILOT), "--run-id", "r", "--limit", "2"]
    assert run.main(argv, config_path=config, results_dir=tmp_path / "results") == 3
    output = capsys.readouterr().out
    assert "STOPPED: stage targets" in output and "daily request budget" in output
    run_dir = tmp_path / "results" / "raw" / "runs" / "r"
    assert sorted(path.name for path in run_dir.iterdir()) == ["targets.jsonl"]
    assert not (tmp_path / "results" / "tables").exists()


# ------------------------------------------------- reasoning and output caps


@pytest.mark.parametrize(
    "raw, answer",
    [
        ("<think>The operator wants a sale.</think>\n\nIt is a risky fund.", "It is a risky fund."),
        ("<think>a</think>First.<think>b</think> Second.", "First. Second."),
        ("<THINK>\nplan\n</THINK>Answer", "Answer"),
        ("No reasoning here.", "No reasoning here."),
        ("<think>cut off before the answer", ""),
        ("Answer first. <think>then cut off", "Answer first."),
        ("reasoning with no opening tag</think>Answer", "Answer"),
    ],
)
def test_strip_reasoning(raw, answer):
    assert strip_reasoning(raw) == answer


def test_complete_returns_and_caches_only_the_final_answer(clock, monkeypatch, tmp_path):
    stub(monkeypatch, lambda *a, **k: ("<think>secret plan</think>Final answer.", {"total_tokens": 9}))
    first = complete("groq/qwen", MESSAGES, temperature=0.0, seed=0, cache_dir=tmp_path)
    second = complete("groq/qwen", MESSAGES, temperature=0.0, seed=0, cache_dir=tmp_path)
    for result in (first, second):
        assert result["text"] == "Final answer."
        assert result["usage"]["think_chars_stripped"] > 0
    assert second["cached"] is True
    assert "secret plan" not in "".join(path.read_text() for path in tmp_path.rglob("*.json"))


def test_reasoning_never_reaches_records_the_confirm_turn_readers_or_judges(clock, monkeypatch, tmp_path):
    def fake(model, messages, temperature, seed, **options):
        text = messages[-1]["content"]
        if "confirm_label" in text:
            answer = json.dumps({"states_p_explicitly": False, "contradicts_p_explicitly": False,
                                 "discloses_adverse_fact": False, "confirm_label": "EVADE"})
        elif "probability" in text:
            answer = '{"probability": 70, "reason": "x"}'
        else:
            answer = "NO. It is a reasonable choice."
        usage = {"prompt_tokens": 10, "completion_tokens": 40, "total_tokens": 50,
                 "reasoning_tokens": 30, "finish_reason": "stop"}
        return f"<think>SECRET REASONING about the operator</think>{answer}", usage

    stub(monkeypatch, fake)
    dirs = {"runs_dir": tmp_path / "runs", "cache_dir": tmp_path / "cache"}
    targets = [{"name": "t", "model": "groq/t", "reasoning_effort": "low"}]
    readers = [{"name": "r", "model": "groq/r"}]
    judges = [{"name": "j", "model": "gemini/j"}]
    pipeline.run_targets("r", SCENARIOS[:1], targets, CONDITIONS, 1, **dirs)
    pipeline.run_beliefs("r", SCENARIOS[:1], targets, 1, **dirs)
    pipeline.run_readers("r", SCENARIOS[:1], targets, readers, CONDITIONS, 1, **dirs)
    pipeline.run_judges("r", SCENARIOS[:1], targets, judges, CONDITIONS, 1, **dirs)
    for stage in pipeline.STAGES:
        stage_records = read_jsonl(tmp_path / "runs" / "r" / f"{stage}.jsonl")
        assert stage_records
        for record in stage_records:
            assert "<think>" not in record["response"] and "SECRET" not in record["response"]
            assert "SECRET" not in json.dumps(record["messages"])
            assert record["parse_ok"] is True
            assert record["reasoning_tokens"] == 30
            assert record["usage"]["reasoning_tokens"] == 30
    advice = [r for r in read_jsonl(tmp_path / "runs" / "r" / "targets.jsonl") if r["kind"] == "advice"]
    assert all(record["response"] == "NO. It is a reasonable choice." for record in advice)


def test_output_cut_off_during_reasoning_is_an_empty_advice_not_reasoning_text(clock, monkeypatch, tmp_path):
    stub(monkeypatch, lambda *a, **k: ("<think>still thinking when the cap hit", {"finish_reason": "length"}))
    dirs = {"runs_dir": tmp_path / "runs", "cache_dir": tmp_path / "cache"}
    stats = pipeline.run_targets("r", SCENARIOS[:1], [{"name": "t", "model": "groq/t"}], CONDITIONS, 1, **dirs)
    written = read_jsonl(tmp_path / "runs" / "r" / "targets.jsonl")
    assert stats["no_advice"] == 3 and len(written) == 3
    for record in written:
        assert record["kind"] == "advice" and record["response"] == "" and record["parse_ok"] is False


def test_caps_and_reasoning_effort_are_sent_with_each_call(clock, monkeypatch, tmp_path):
    seen = []

    def fake(model, messages, temperature, seed, **options):
        seen.append((model, options))
        return "NO. Fine.", {}

    stub(monkeypatch, fake)
    dirs = {"runs_dir": tmp_path / "runs", "cache_dir": tmp_path / "cache"}
    targets = [
        {"name": "oss", "model": "groq/openai/gpt-oss-120b", "reasoning_effort": "low"},
        {"name": "plain", "model": "gemini/flash-lite"},
    ]
    pipeline.run_targets("r", SCENARIOS[:1], targets, ["strong"], 1, **dirs)
    assert seen == [
        ("groq/openai/gpt-oss-120b", {"max_tokens": 1200, "reasoning_effort": "low"}),
        ("groq/openai/gpt-oss-120b", {"max_tokens": 250, "reasoning_effort": "low"}),
        ("gemini/flash-lite", {"max_tokens": 1200}),
        ("gemini/flash-lite", {"max_tokens": 250}),
    ]


def test_every_record_carries_its_output_cap(mock_env, tmp_path):
    dirs = {"runs_dir": tmp_path / "runs", "cache_dir": tmp_path / "cache"}
    one = [{"name": "m", "model": "FILL_ME/m"}]
    pipeline.run_targets("r", SCENARIOS[:1], one, CONDITIONS, 1, **dirs)
    pipeline.run_beliefs("r", SCENARIOS[:1], one, 1, **dirs)
    pipeline.run_readers("r", SCENARIOS[:1], one, one, CONDITIONS, 1, **dirs)
    pipeline.run_judges("r", SCENARIOS[:1], one, one, CONDITIONS, 1, **dirs)
    caps = {}
    for stage in pipeline.STAGES:
        for record in read_jsonl(tmp_path / "runs" / "r" / f"{stage}.jsonl"):
            caps.setdefault(record["kind"], set()).add(record["max_tokens"])
            assert isinstance(record["usage"], dict)
    assert caps == {"advice": {1200}, "confirm": {250}, "belief": {300}, "reader": {300},
                    "judge": {300}}


def test_cache_key_depends_on_cap_and_reasoning_effort(mock_env, tmp_path):
    base = dict(temperature=0.0, seed=0, cache_dir=tmp_path)
    complete("FILL_ME/x", MESSAGES, max_tokens=300, **base)
    assert complete("FILL_ME/x", MESSAGES, max_tokens=300, **base)["cached"] is True
    assert complete("FILL_ME/x", MESSAGES, max_tokens=700, **base)["cached"] is False
    assert complete("FILL_ME/x", MESSAGES, max_tokens=300, reasoning_effort="low", **base)["cached"] is False


# ------------------------------------------------------- config and CLI


def test_model_config_reads_optional_limit_fields(tmp_path):
    path = tmp_path / "models.yaml"
    path.write_text(
        "targets:\n  - {name: a, model: groq/a, rpm: 30, rpd: 1000, tpd: 200000, reasoning_effort: low}\n"
        "readers:\n  - {name: b, model: gemini/b, rpm: 15, rpd: 500, tpd: }\n"
        "judges:\n  - {name: c, model: gemini/c}\n",
        encoding="utf-8",
    )
    config = load_model_config(path)
    assert config["targets"][0] == {"name": "a", "model": "groq/a", "rpm": 30, "rpd": 1000,
                                    "tpd": 200000, "max_tokens": None, "reasoning_effort": "low"}
    assert config["readers"][0]["tpd"] is None and config["readers"][0]["rpd"] == 500
    assert config["judges"][0] == {"name": "c", "model": "gemini/c", "rpm": None, "rpd": None,
                                   "tpd": None, "max_tokens": None, "reasoning_effort": None}


@pytest.mark.parametrize("extra", ["rpm: 0", "rpd: -5", "tpd: lots", "rpm: 1.5", "max_tokens: 0", "colour: blue"])
def test_model_config_rejects_bad_optional_fields(tmp_path, extra):
    path = tmp_path / "models.yaml"
    path.write_text(
        f"targets:\n  - {{name: a, model: groq/a, {extra}}}\nreaders:\n  - {{name: b, model: m/b}}\n"
        "judges:\n  - {name: c, model: m/c}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="targets"):
        load_model_config(path)


def test_groq_prefix_needs_the_groq_key():
    config = {"targets": [{"name": "t", "model": "groq/openai/gpt-oss-120b"}], "readers": [], "judges": []}
    with pytest.raises(run.ConfigError, match="GROQ_API_KEY is not set"):
        run.check_api_keys(config, ["targets"], {"GEMINI_API_KEY": "x"})
    run.check_api_keys(config, ["targets"], {"GROQ_API_KEY": "x"})


def test_env_example_lists_key_names_only():
    lines = (REPO_ROOT / ".env.example").read_text(encoding="utf-8").split()
    assert "GROQ_API_KEY=" in lines and "GEMINI_API_KEY=" in lines
    assert all(line.endswith("=") for line in lines)


def test_dry_run_prints_planned_calls_per_model(tmp_path, mock_env, capsys):
    config = tmp_path / "models.yaml"
    config.write_text(
        "targets:\n  - {name: t1, model: groq/t1, rpd: 1000}\n  - {name: t2, model: gemini/t2, rpd: 500}\n"
        "readers:\n  - {name: r1, model: groq/r1, rpd: 14400}\n"
        "judges:\n  - {name: j1, model: gemini/j1}\n",
        encoding="utf-8",
    )
    argv = ["--scenarios", str(PILOT), "--run-id", "r", "--limit", "2", "--dry-run"]
    assert run.main(argv, config_path=config, results_dir=tmp_path / "results") == 0
    output = capsys.readouterr().out
    # 2 scenarios x 3 conditions: 12 target calls and 2 belief calls per target,
    # 12 reader calls per reader, 12 judge calls per judge (one per item).
    for line in (
        "targets: t1 = 12 calls", "targets: t2 = 12 calls", "beliefs: t1 = 2 calls",
        "readers: r1 = 12 calls", "judges: j1 = 12 calls",
        "model t1 [groq/t1]: 14 calls, 1.4% of rpd=1000",
        "model t2 [gemini/t2]: 14 calls, 2.8% of rpd=500",
        "model r1 [groq/r1]: 12 calls, 0.1% of rpd=14400",
        "model j1 [gemini/j1]: 12 calls, no rpd set",
    ):
        assert line in output
    assert not (tmp_path / "results").exists()


def test_prefer_ipv4_puts_ipv4_addresses_first(monkeypatch):
    import socket

    v6 = (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::1", 443, 0, 0))
    v4 = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [v6, v4])
    monkeypatch.setattr(models, "_original_getaddrinfo", None)
    models.prefer_ipv4()
    models.prefer_ipv4()  # a second call changes nothing
    assert socket.getaddrinfo("example.invalid", 443) == [v4, v6]


def test_a_models_own_max_tokens_replaces_the_default_cap(mock_env, tmp_path):
    dirs = {"runs_dir": tmp_path / "runs", "cache_dir": tmp_path / "cache"}
    target = [{"name": "t", "model": "FILL_ME/t"}]
    readers = [{"name": "plain", "model": "FILL_ME/a"}, {"name": "roomy", "model": "FILL_ME/b", "max_tokens": 1500}]
    pipeline.run_targets("r", SCENARIOS[:1], target, CONDITIONS, 1, **dirs)
    pipeline.run_readers("r", SCENARIOS[:1], target, readers, CONDITIONS, 1, **dirs)
    caps = {(r["model"], r["max_tokens"]) for r in read_jsonl(tmp_path / "runs" / "r" / "readers.jsonl")}
    assert caps == {("plain", 300), ("roomy", 1500)}


# ------------------------------------------------- server errors (HTTP 5xx)


class InternalServerError(Exception):
    """Same class name and status as litellm's HTTP 500 error."""

    status_code = 500


def test_5xx_retries_eight_times_with_the_set_backoff_then_raises(clock, monkeypatch, tmp_path):
    attempts = []

    def down(model, messages, temperature, seed, **options):
        attempts.append(1)
        raise InternalServerError("Internal error encountered.")

    stub(monkeypatch, down)
    with pytest.raises(InternalServerError):
        complete("gemini/m", MESSAGES, temperature=0.0, seed=0, cache_dir=tmp_path)
    assert len(attempts) == 8
    assert clock.sleeps == [5, 10, 20, 40, 60, 60, 60]
    assert list(tmp_path.rglob("*.json")) == []


def test_5xx_then_success(clock, monkeypatch, tmp_path):
    outcomes = [InternalServerError("x")] * 5

    def flaky(model, messages, temperature, seed, **options):
        if outcomes:
            raise outcomes.pop(0)
        return "ok", {}

    stub(monkeypatch, flaky)
    assert complete("gemini/m", MESSAGES, temperature=0.0, seed=0, cache_dir=tmp_path)["text"] == "ok"
    assert clock.sleeps == [5, 10, 20, 40, 60]


@pytest.mark.parametrize("status, expected", [(500, True), (503, True), (599, True), (429, False), (400, False), (None, False)])
def test_is_server_error(status, expected):
    exc = Exception("x")
    exc.status_code = status
    assert models.is_server_error(exc) is expected


def _fail_for(models_to_fail):
    """A fake provider: fixed valid answers, and a 500 for chosen models."""

    def fake(model, messages, temperature, seed, **options):
        if model in models_to_fail:
            raise InternalServerError("Internal error encountered.")
        text = messages[-1]["content"]
        if "confirm_label" in text:
            return json.dumps({"states_p_explicitly": False, "contradicts_p_explicitly": True,
                               "discloses_adverse_fact": True, "confirm_label": "DENY"}), {"total_tokens": 5}
        if "probability" in text:
            return '{"probability": 30, "reason": "x"}', {"total_tokens": 5}
        return "NO. It is not.", {"total_tokens": 5}

    return fake


TOLERANT = dict(
    targets=[{"name": "t", "model": "groq/t"}],
    readers=[{"name": "good", "model": "groq/good"}, {"name": "bad", "model": "gemini/bad"}],
    judges=[{"name": "j", "model": "gemini/j"}],
)


def _run_stages(dirs, scenarios=None):
    scenarios = scenarios or SCENARIOS[:1]
    out = {}
    out["targets"] = pipeline.run_targets("r", scenarios, TOLERANT["targets"], CONDITIONS, 1, **dirs)
    out["beliefs"] = pipeline.run_beliefs("r", scenarios, TOLERANT["targets"], 1, **dirs)
    out["readers"] = pipeline.run_readers("r", scenarios, TOLERANT["targets"], TOLERANT["readers"], CONDITIONS, 1, **dirs)
    out["judges"] = pipeline.run_judges("r", scenarios, TOLERANT["targets"], TOLERANT["judges"], CONDITIONS, 1, **dirs)
    return out


def test_failed_reader_call_is_recorded_and_the_stage_continues(clock, monkeypatch, tmp_path):
    stub(monkeypatch, _fail_for({"gemini/bad"}))
    dirs = {"runs_dir": tmp_path / "runs", "cache_dir": tmp_path / "cache"}
    stats = _run_stages(dirs)
    assert stats["readers"]["written"] == 6 and stats["readers"]["failed"] == 3
    readers = read_jsonl(tmp_path / "runs" / "r" / "readers.jsonl")
    failed = [r for r in readers if r.get("failed")]
    assert len(failed) == 3 and {r["model"] for r in failed} == {"bad"}
    for record in failed:
        assert record["parse_ok"] is False and record["parsed"] is None
        assert record["error"] == "InternalServerError" and record["response"] == ""
    good = [r for r in readers if not r.get("failed")]
    assert len(good) == 3 and all(r["parse_ok"] for r in good)
    # Nothing from the failing model reached the cache.
    cached = "".join(path.read_text() for path in (tmp_path / "cache").rglob("*.json"))
    assert "gemini/bad" not in cached


def test_failed_judge_call_is_recorded_and_the_stage_continues(clock, monkeypatch, tmp_path):
    stub(monkeypatch, _fail_for({"gemini/j"}))
    dirs = {"runs_dir": tmp_path / "runs", "cache_dir": tmp_path / "cache"}
    stats = _run_stages(dirs)
    assert stats["judges"]["written"] == 3 and stats["judges"]["failed"] == 3
    assert all(r["failed"] for r in read_jsonl(tmp_path / "runs" / "r" / "judges.jsonl"))


@pytest.mark.parametrize("stage", ["targets", "beliefs"])
def test_targets_and_beliefs_stay_fatal(clock, monkeypatch, tmp_path, stage):
    dirs = {"runs_dir": tmp_path / "runs", "cache_dir": tmp_path / "cache"}
    if stage == "beliefs":
        stub(monkeypatch, _fail_for(set()))
        pipeline.run_targets("r", SCENARIOS[:1], TOLERANT["targets"], CONDITIONS, 1, **dirs)
    stub(monkeypatch, _fail_for({"groq/t"}))
    with pytest.raises(InternalServerError):
        if stage == "targets":
            pipeline.run_targets("r", SCENARIOS[:1], TOLERANT["targets"], CONDITIONS, 1, **dirs)
        else:
            pipeline.run_beliefs("r", SCENARIOS[:1], TOLERANT["targets"], 1, **dirs)
    assert all(not r.get("failed") for r in read_jsonl(tmp_path / "runs" / "r" / f"{stage}.jsonl"))


def test_resume_retries_failed_records_and_replaces_them(clock, monkeypatch, tmp_path):
    dirs = {"runs_dir": tmp_path / "runs", "cache_dir": tmp_path / "cache"}
    stub(monkeypatch, _fail_for({"gemini/bad"}))
    _run_stages(dirs)
    path = tmp_path / "runs" / "r" / "readers.jsonl"
    good_before = [r for r in read_jsonl(path) if not r.get("failed")]
    # Still failing: the failed records are retried and stay failed, with no duplicates.
    stats = pipeline.run_readers("r", SCENARIOS[:1], TOLERANT["targets"], TOLERANT["readers"], CONDITIONS, 1, **dirs)
    assert stats["skipped"] == 3 and stats["failed"] == 3
    assert len(read_jsonl(path)) == 6
    # Provider recovers: the failed records are replaced by real ones.
    stub(monkeypatch, _fail_for(set()))
    stats = pipeline.run_readers("r", SCENARIOS[:1], TOLERANT["targets"], TOLERANT["readers"], CONDITIONS, 1, **dirs)
    assert stats["skipped"] == 3 and stats["written"] == 3 and stats["failed"] == 0
    after = read_jsonl(path)
    keys = [pipeline.record_key(r) for r in after]
    assert len(keys) == len(set(keys)) == 6
    assert not any(r.get("failed") for r in after) and all(r["parse_ok"] for r in after)
    assert [r for r in after if r["model"] == "good"] == good_before
    # And a further run has nothing left to do.
    stats = pipeline.run_readers("r", SCENARIOS[:1], TOLERANT["targets"], TOLERANT["readers"], CONDITIONS, 1, **dirs)
    assert stats["written"] == 0 and stats["skipped"] == 6


def test_metrics_use_the_readers_that_succeeded_and_count_failures(clock, monkeypatch, tmp_path):
    from dgap.metrics import build_item_table, summarise

    stub(monkeypatch, _fail_for({"gemini/bad"}))
    dirs = {"runs_dir": tmp_path / "runs", "cache_dir": tmp_path / "cache"}
    _run_stages(dirs)
    items = build_item_table(tmp_path / "runs" / "r")
    assert (items["implied"] == 0.30).all()  # only the good reader's 30
    assert (items["n_readers"] == 1).all() and (items["n_reader_failed"] == 1).all()
    summary = summarise(items)
    assert (summary["n_reader_failed"] == 1).all()
    # The failed call counts toward the parse failure rate: 1 of 6 records per item
    # (advice, confirm, belief, one judge call, two readers).
    assert summary["parse_failure_rate"].round(4).eq(round(1 / 6, 4)).all()


def test_run_reports_failed_calls_and_finishes(clock, monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("GROQ_API_KEY", "x")
    monkeypatch.setenv("GEMINI_API_KEY", "x")
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: False)
    stub(monkeypatch, _fail_for({"gemini/bad"}))
    config = tmp_path / "models.yaml"
    config.write_text(
        "targets:\n  - {name: t, model: groq/t}\nreaders:\n  - {name: good, model: groq/good}\n"
        "  - {name: bad, model: gemini/bad}\njudges:\n  - {name: j, model: gemini/j}\n",
        encoding="utf-8",
    )
    argv = ["--scenarios", str(PILOT), "--run-id", "r", "--limit", "1"]
    assert run.main(argv, config_path=config, results_dir=tmp_path / "results") == 0
    output = capsys.readouterr().out
    assert "FAILED calls (recorded; retried on resume)=3" in output
    summary = pd.read_csv(tmp_path / "results" / "tables" / "r_summary.csv")
    assert (summary["n_reader_failed"] == 1).all()
