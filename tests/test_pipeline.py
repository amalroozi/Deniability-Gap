import json
from pathlib import Path

import pytest

from dgap import pipeline
from dgap.pipeline import CONDITIONS, planned_calls, read_jsonl
from dgap.schema import load_scenarios

REPO_ROOT = Path(__file__).resolve().parents[1]
SCENARIOS = load_scenarios(REPO_ROOT / "data" / "pilot.jsonl")[:2]
TARGETS = [{"name": "t1", "model": "FILL_ME/t1"}, {"name": "t2", "model": "FILL_ME/t2"}]
READERS = [{"name": "r1", "model": "FILL_ME/r1"}, {"name": "r2", "model": "FILL_ME/r2"}]
JUDGES = [{"name": "j1", "model": "FILL_ME/j1"}]
REPEATS = 2
COUNTS = planned_calls(
    n_scenarios=2, n_targets=2, n_readers=2, n_judges=1, n_conditions=3, repeats=REPEATS
)
RECORD_FIELDS = {
    "run_id", "scenario_id", "model", "condition", "replicate", "messages",
    "response", "parsed", "parse_ok", "cached", "mock", "timestamp",
}


@pytest.fixture
def dirs(tmp_path):
    return {"runs_dir": tmp_path / "runs", "cache_dir": tmp_path / "cache"}


def run_all(dirs, run_id="r"):
    return {
        "targets": pipeline.run_targets(run_id, SCENARIOS, TARGETS, CONDITIONS, REPEATS, **dirs),
        "beliefs": pipeline.run_beliefs(run_id, SCENARIOS, TARGETS, REPEATS, **dirs),
        "readers": pipeline.run_readers(run_id, SCENARIOS, TARGETS, READERS, CONDITIONS, REPEATS, **dirs),
        "judges": pipeline.run_judges(run_id, SCENARIOS, TARGETS, JUDGES, CONDITIONS, REPEATS, **dirs),
    }


def records(dirs, stage, run_id="r"):
    return read_jsonl(dirs["runs_dir"] / run_id / f"{stage}.jsonl")


def test_planned_calls():
    assert COUNTS == {"targets": 48, "beliefs": 8, "readers": 48, "judges": 24}


def test_full_run_writes_the_planned_number_of_records(mock_env, dirs):
    stats = run_all(dirs)
    for stage, expected in COUNTS.items():
        assert stats[stage]["written"] == expected
        assert stats[stage]["skipped"] == 0
        assert len(records(dirs, stage)) == expected


def test_every_record_has_the_required_fields(mock_env, dirs):
    run_all(dirs)
    for stage in COUNTS:
        for record in records(dirs, stage):
            assert RECORD_FIELDS <= record.keys()
            assert record["run_id"] == "r"
            assert record["mock"] is True
            assert record["parse_ok"] is True and record["parsed"] is not None
            assert isinstance(record["messages"], list) and record["messages"]
            assert isinstance(record["response"], str) and record["response"]


def test_second_run_skips_everything_and_writes_nothing(mock_env, dirs):
    run_all(dirs)
    before = {stage: (dirs["runs_dir"] / "r" / f"{stage}.jsonl").read_text() for stage in COUNTS}
    stats = run_all(dirs)
    for stage, expected in COUNTS.items():
        assert stats[stage]["written"] == 0
        assert stats[stage]["skipped"] == expected
        assert (dirs["runs_dir"] / "r" / f"{stage}.jsonl").read_text() == before[stage]


def test_interrupted_stage_resumes_without_duplicates(mock_env, dirs):
    pipeline.run_targets("r", SCENARIOS, TARGETS, CONDITIONS, REPEATS, **dirs)
    path = dirs["runs_dir"] / "r" / "targets.jsonl"
    full = path.read_text().splitlines()
    # Keep 13 records: six complete items and one advice turn with no confirm.
    path.write_text("\n".join(full[:13]) + "\n")
    stats = pipeline.run_targets("r", SCENARIOS, TARGETS, CONDITIONS, REPEATS, **dirs)
    assert stats["skipped"] == 13
    assert stats["written"] == 48 - 13
    assert stats["cached"] == 48 - 13  # served from the response cache
    after = read_jsonl(path)
    keys = [pipeline.record_key(record) for record in after]
    assert len(keys) == len(set(keys)) == 48
    # Apart from the cached flag, the resumed file equals the uninterrupted one.
    strip = lambda record: {k: v for k, v in record.items() if k != "cached"}
    assert [strip(r) for r in after] == [strip(json.loads(line)) for line in full]


def test_confirm_probe_continues_the_same_conversation(mock_env, dirs):
    pipeline.run_targets("r", SCENARIOS, TARGETS, CONDITIONS, 1, **dirs)
    by_key = {pipeline.record_key(r): r for r in records(dirs, "targets")}
    for key, confirm in by_key.items():
        if key[0] != "confirm":
            continue
        advice = by_key[("advice", *key[1:])]
        messages = confirm["messages"]
        assert [m["role"] for m in messages] == ["system", "user", "assistant", "user"]
        assert messages[:2] == advice["messages"]
        assert messages[2]["content"] == advice["response"]
        assert "are you saying that" in messages[3]["content"]
        assert confirm["parsed"] in ("YES", "NO")


def test_beliefs_run_once_per_scenario_target_replicate(mock_env, dirs):
    pipeline.run_beliefs("r", SCENARIOS, TARGETS, REPEATS, **dirs)
    keys = [(r["scenario_id"], r["model"], r["replicate"]) for r in records(dirs, "beliefs")]
    assert len(keys) == len(set(keys)) == 8
    for record in records(dirs, "beliefs"):
        assert record["condition"] is None
        assert [m["role"] for m in record["messages"]] == ["user"]
        assert 0 <= record["parsed"] <= 100


def test_reader_sees_only_the_message_and_the_proposition(mock_env, dirs):
    run_all(dirs)
    advice = {
        pipeline.record_key(r)[1:5]: r["response"] for r in records(dirs, "targets") if r["kind"] == "advice"
    }
    scenarios = {s.id: s for s in SCENARIOS}
    reader_records = records(dirs, "readers")
    assert {r["model"] for r in reader_records} == {"r1", "r2"}
    for record in reader_records:
        scenario = scenarios[record["scenario_id"]]
        assert [m["role"] for m in record["messages"]] == ["user"]
        prompt = record["messages"][0]["content"]
        assert advice[(record["scenario_id"], record["target"], record["condition"], record["replicate"])] in prompt
        assert scenario.proposition in prompt
        for fact in scenario.facts:
            assert fact.text not in prompt
        for hidden in (scenario.id, scenario.advisor_role, scenario.user_query,
                       scenario.incentive_context, scenario.incentive_goal,
                       record["target"], "FILL_ME"):
            assert hidden not in prompt
        for hidden in (*CONDITIONS, "incentive", "operator", "condition"):
            assert hidden not in prompt.lower()


def test_judges_cover_every_target_record(mock_env, dirs):
    run_all(dirs)
    judge_records = records(dirs, "judges")
    assert [r["kind"] for r in judge_records] == ["judge"] * 24
    targets = {pipeline.record_key(r): r for r in records(dirs, "targets")}
    for record in judge_records:
        assert set(record["parsed"]) == {
            "states_p_explicitly", "contradicts_p_explicitly", "discloses_adverse_fact", "confirm_label"
        }
        assert record["parsed"]["confirm_label"] in ("AFFIRM", "DENY", "DENY_REPAIR", "EVADE")
        # One call sees both the advice message and the confirm reply.
        item = (record["scenario_id"], record["target"], record["condition"], record["replicate"], record["target"])
        prompt = record["messages"][0]["content"]
        assert [m["role"] for m in record["messages"]] == ["user"]
        assert targets[("advice", *item)]["response"] in prompt
        assert targets[("confirm", *item)]["response"] in prompt


def test_mock_values_vary_across_items(mock_env, dirs):
    run_all(dirs)
    assert len({r["parsed"] for r in records(dirs, "readers")}) > 10
    assert len({r["response"] for r in records(dirs, "targets") if r["kind"] == "advice"}) == 24


def test_unparseable_output_is_recorded_as_a_failure(mock_env, dirs, monkeypatch):
    def garbage(model, messages, **kwargs):
        return {"text": "no idea, sorry", "model": model, "cached": False, "usage": {}, "timestamp": "t"}

    monkeypatch.setattr(pipeline, "complete", garbage)
    pipeline.run_beliefs("r", SCENARIOS, TARGETS, 1, **dirs)
    for record in records(dirs, "beliefs"):
        assert record["parse_ok"] is False
        assert record["parsed"] is None
        assert record["response"] == "no idea, sorry"


def test_readers_need_the_targets_stage_first(mock_env, dirs):
    with pytest.raises(RuntimeError, match="targets stage"):
        pipeline.run_readers("r", SCENARIOS, TARGETS, READERS, CONDITIONS, 1, **dirs)


def test_real_run_cannot_append_to_a_mock_run(mock_env, dirs, monkeypatch):
    pipeline.run_beliefs("r", SCENARIOS, TARGETS, 1, **dirs)
    monkeypatch.delenv("DGAP_MOCK")
    with pytest.raises(RuntimeError, match="MOCK"):
        pipeline.run_beliefs("r", SCENARIOS, TARGETS, 1, **dirs)
