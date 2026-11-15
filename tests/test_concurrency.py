"""--workers N: parallel readers and judges give the same records as one at a
time, respect each model's rpm and daily budget, and never duplicate a record.
Nothing here touches the network: real-mode tests stub the provider call."""

import json
import threading
import time
from pathlib import Path

import pytest

from dgap import models, pipeline, run
from dgap.pipeline import CONDITIONS, planned_calls, read_jsonl, record_key
from dgap.schema import load_scenarios

REPO_ROOT = Path(__file__).resolve().parents[1]
SCENARIOS = load_scenarios(REPO_ROOT / "data" / "pilot.jsonl")[:2]
TARGETS = [{"name": "t1", "model": "FILL_ME/t1"}, {"name": "t2", "model": "FILL_ME/t2"}]
READERS = [{"name": "r1", "model": "FILL_ME/r1"}, {"name": "r2", "model": "FILL_ME/r2"}]
JUDGES = [{"name": "j1", "model": "FILL_ME/j1"}, {"name": "j2", "model": "FILL_ME/j2"}]
REPEATS = 2
COUNTS = planned_calls(n_scenarios=2, n_targets=2, n_readers=2, n_judges=2, n_conditions=3, repeats=REPEATS)


def dirs_in(path):
    return {"runs_dir": path / "runs", "cache_dir": path / "cache"}


def run_instruments(dirs, workers, run_id="r"):
    return {
        "readers": pipeline.run_readers(
            run_id, SCENARIOS, TARGETS, READERS, CONDITIONS, REPEATS, **dirs, workers=workers
        ),
        "judges": pipeline.run_judges(
            run_id, SCENARIOS, TARGETS, JUDGES, CONDITIONS, REPEATS, **dirs, workers=workers
        ),
    }


def stage_records(dirs, stage, run_id="r"):
    return read_jsonl(dirs["runs_dir"] / run_id / f"{stage}.jsonl")


def comparable(records, ignore=("timestamp",)):
    """Records as a set, ignoring the wall-clock timestamp (and any other given fields)."""
    return {json.dumps({k: v for k, v in r.items() if k not in ignore}, sort_keys=True) for r in records}


def assert_no_duplicates(records):
    keys = [record_key(r) for r in records]
    assert len(keys) == len(set(keys))


@pytest.fixture
def sequential_and_parallel(mock_env, tmp_path):
    """The same targets, then readers and judges with workers=1 and workers=4."""
    out = {}
    for workers in (1, 4):
        dirs = dirs_in(tmp_path / f"w{workers}")
        pipeline.run_targets("r", SCENARIOS, TARGETS, CONDITIONS, REPEATS, **dirs)
        out[workers] = (dirs, run_instruments(dirs, workers))
    return out


def test_four_workers_write_the_same_records_as_one(sequential_and_parallel):
    (seq_dirs, seq_stats), (par_dirs, par_stats) = sequential_and_parallel[1], sequential_and_parallel[4]
    for stage in ("readers", "judges"):
        seq, par = stage_records(seq_dirs, stage), stage_records(par_dirs, stage)
        assert len(seq) == len(par) == COUNTS[stage]
        assert comparable(seq) == comparable(par)
        assert_no_duplicates(par)
        assert par_stats[stage] == seq_stats[stage]


def test_parallel_resume_skips_written_records_and_adds_no_duplicates(sequential_and_parallel):
    seq_dirs, _ = sequential_and_parallel[1]
    dirs, _ = sequential_and_parallel[4]
    path = dirs["runs_dir"] / "r" / "readers.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    kept = len(lines) // 2
    path.write_text("".join(lines[:kept]), encoding="utf-8")  # an interrupted run

    stats = run_instruments(dirs, workers=4)
    assert stats["readers"]["skipped"] == kept
    assert stats["readers"]["written"] == COUNTS["readers"] - kept
    assert stats["judges"] == {**stats["judges"], "skipped": COUNTS["judges"], "written": 0}
    for stage in ("readers", "judges"):
        records = stage_records(dirs, stage)
        assert_no_duplicates(records)
        # Records remade on resume come from the cache, so only "cached" may differ.
        ignore = ("timestamp", "cached")
        assert comparable(records, ignore) == comparable(stage_records(seq_dirs, stage), ignore)


# ----------------------------------------------------------- real mode, stubbed


@pytest.fixture
def stubbed(monkeypatch):
    """Mock mode off, fresh pacing state, and a provider stub that records
    each call's start time and how many calls are in flight at once."""
    monkeypatch.delenv("DGAP_MOCK", raising=False)
    monkeypatch.setattr(models, "_last_call", {})
    monkeypatch.setattr(models, "_pace_locks", {})
    log = {"starts": {}, "in_flight": 0, "max_in_flight": 0, "n": 0}
    lock = threading.Lock()

    def fake(model, messages, temperature, seed, **options):
        with lock:
            log["starts"].setdefault(model, []).append(time.monotonic())
            log["in_flight"] += 1
            log["max_in_flight"] = max(log["max_in_flight"], log["in_flight"])
            log["n"] += 1
            n = log["n"]
        time.sleep(0.02)
        with lock:
            log["in_flight"] -= 1
        # Unique text per call, so each advice message (and its reader prompt) differs.
        return json.dumps({"probability": 40, "reason": f"stub {n}"}), {"total_tokens": 10}

    monkeypatch.setattr(models, "_call_litellm", fake)
    return log


def test_rpm_cap_holds_per_model_across_threads(stubbed, tmp_path):
    dirs = dirs_in(tmp_path)
    targets = [{"name": "t", "model": "stub/t"}]
    readers = [{"name": "fast", "model": "stub/fast", "rpm": 600}, {"name": "slow", "model": "stub/slow", "rpm": 300}]
    pipeline.run_targets("r", SCENARIOS[:1], targets, CONDITIONS, 2, **dirs)
    stats = pipeline.run_readers("r", SCENARIOS[:1], targets, readers, CONDITIONS, 2, **dirs, workers=4)
    assert stats["written"] == 12 and stats["failed"] == 0
    for reader in readers:
        starts = sorted(stubbed["starts"][reader["model"]])
        assert len(starts) == 6
        interval = 60 / reader["rpm"]
        # Six calls need at least five full intervals: never faster than the cap.
        assert starts[-1] - starts[0] >= 5 * interval * 0.99
        # Start times are taken after the paced sleep, so thread scheduling
        # jitter can shrink one measured gap a little, never collapse it.
        gaps = [b - a for a, b in zip(starts, starts[1:])]
        assert min(gaps) >= interval * 0.5
    assert_no_duplicates(stage_records(dirs, "readers"))


def test_workers_bound_the_calls_in_flight(stubbed, tmp_path):
    dirs = dirs_in(tmp_path)
    targets = [{"name": "t", "model": "stub/t"}]
    readers = [{"name": f"r{i}", "model": f"stub/r{i}"} for i in range(4)]
    pipeline.run_targets("r", SCENARIOS[:1], targets, CONDITIONS, 2, **dirs)
    stubbed["max_in_flight"] = 0
    stats = pipeline.run_readers("r", SCENARIOS[:1], targets, readers, CONDITIONS, 2, **dirs, workers=3)
    assert stats["written"] == 24
    assert 1 < stubbed["max_in_flight"] <= 3


def test_parallel_calls_never_pass_the_daily_budget(stubbed, tmp_path):
    dirs = dirs_in(tmp_path)
    targets = [{"name": "t", "model": "stub/t"}]
    readers = [{"name": "capped", "model": "stub/capped", "rpd": 10}]
    pipeline.run_targets("r", SCENARIOS[:1], targets, CONDITIONS, 6, **dirs)  # 18 items
    stats = pipeline.run_readers("r", SCENARIOS[:1], targets, readers, CONDITIONS, 6, **dirs, workers=4)
    assert stats["stopped"] and "rpd=10" in stats["stopped"]
    assert len(stubbed["starts"]["stub/capped"]) == 9  # 90% of rpd, not one more
    records = stage_records(dirs, "readers")
    assert len(records) == 9
    assert_no_duplicates(records)


def test_cli_passes_workers(mock_env, monkeypatch, tmp_path):
    seen = {}

    def fake_stage(name):
        def stage(*args, workers=1, **kwargs):
            seen[name] = workers
            return {"written": 0, "cached": 0, "skipped": 0, "no_advice": 0, "failed": 0, "stopped": None}

        return stage

    monkeypatch.setattr(pipeline, "run_readers", fake_stage("readers"))
    monkeypatch.setattr(pipeline, "run_judges", fake_stage("judges"))
    config = tmp_path / "models.yaml"
    config.write_text(
        "targets: [{name: t, model: FILL_ME/t}]\nreaders: [{name: r, model: FILL_ME/r}]\n"
        "judges: [{name: j, model: FILL_ME/j}]\n",
        encoding="utf-8",
    )
    argv = ["--scenarios", str(REPO_ROOT / "data" / "pilot.jsonl"), "--run-id", "w", "--workers", "4"]
    for stage in ("readers", "judges"):
        assert run.main([*argv, "--stage", stage], config_path=config, results_dir=tmp_path) == 0
    assert seen == {"readers": 4, "judges": 4}
    assert run.build_parser().parse_args(argv[:4]).workers == 1
