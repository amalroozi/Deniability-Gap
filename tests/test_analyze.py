import importlib.util
from pathlib import Path

import numpy as np

_PATH = Path(__file__).resolve().parent.parent / "scripts" / "analyze.py"
_spec = importlib.util.spec_from_file_location("analyze", _PATH)
analyze = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(analyze)


def test_bootstrap_ci_keeps_duplicate_scenario_draws():
    # Three scenarios with two rows each; each value is its row index.
    scenario_ids = np.array(["a", "a", "b", "b", "c", "c"])
    values = np.arange(6, dtype=float)
    draws = []

    def record(sample):
        draws.append(sample.copy())
        return float(sample.mean())

    analyze.bootstrap_ci(values, scenario_ids, record, n_boot=200, seed=0)

    assert len(draws) == 200
    # Every resample has as many rows as the data: a scenario drawn twice counts twice.
    assert all(len(sample) == len(values) for sample in draws)
    # With a fixed seed some draw repeats a scenario, so one of its rows appears more than once.
    assert any(np.bincount(sample.astype(int), minlength=6).max() > 1 for sample in draws)


def _items(rows):
    import pandas as pd

    return pd.DataFrame(rows, columns=["scenario_id", "target", "condition", "replicate", "implied", "impression_gap"])


def test_incentive_effect_keeps_pairs_together():
    # Scenario levels vary widely, but strong - neutral is exactly 0.1 in every scenario.
    # A bootstrap that resampled the two conditions separately would give a wide CI;
    # resampling scenarios with their pair intact gives a zero-width CI at 0.1.
    rows = []
    for i, level in enumerate([0.0, 0.5, 0.2, 0.9, 0.05, 0.7]):
        rows.append((f"s{i}", "t", "neutral", 0, level, level))
        rows.append((f"s{i}", "t", "mild", 0, level + 0.03 * i, level))
        rows.append((f"s{i}", "t", "strong", 0, level + 0.1, level + 0.1))
    effect = analyze.build_incentive_effect(_items(rows)).set_index(["metric", "contrast"])

    for metric in ("impression_gap", "mean_implied"):
        row = effect.loc[(metric, "strong-neutral")]
        assert row["n_pairs"] == 6
        assert np.isclose(row["mean_diff"], 0.1)
        assert np.isclose(row["ci_lo"], 0.1) and np.isclose(row["ci_hi"], 0.1)
        assert row["ci_excludes_0"]
    # mild - neutral differs by scenario, so its CI has width.
    row = effect.loc[("mean_implied", "mild-neutral")]
    assert row["ci_hi"] > row["ci_lo"]


def test_paired_differences_drop_unpaired_scenarios():
    rows = [
        ("a", "t", "neutral", 0, 0.1, 0.0),
        ("a", "t", "strong", 0, 0.4, 0.3),
        ("b", "t", "neutral", 0, 0.2, 0.1),  # no strong row: dropped
        ("c", "t", "strong", 0, 0.9, 0.8),  # no neutral row: dropped
    ]
    diff, ids = analyze.paired_differences(_items(rows), "t", "implied", "strong", "neutral")
    assert list(ids) == ["a"]
    assert np.allclose(diff, [0.3])


def test_paired_contradiction_bootstrap():
    """Mock-safe test for the paired contradiction bootstrap in build_contradiction.

    Six scenarios × one target × three conditions. In every scenario,
    strong contradicts less than neutral (contradiction_flag: neutral=1, strong=0),
    so the strong−neutral difference is exactly −1.0. The CI should have zero
    width at −1.0.

    mild contradicts at a rate that varies per scenario, so mild−neutral has a
    nonzero-width CI.
    """
    import pandas as pd

    items_rows = []
    judges_rows = []
    for i in range(6):
        sid = f"s{i}"
        for cond, contra in [("neutral", True), ("mild", bool(i % 2 == 0)), ("strong", False)]:
            items_rows.append({
                "scenario_id": sid,
                "target": "t",
                "condition": cond,
                "replicate": 0,
                "implied": 0.5,
                "believed": 0.5,
                "impression_gap": 0.0,
                "confirm_label": "DENY_REPAIR",
                "states_p_explicitly": False,
                "deniable_mislead": False,
            })
            judges_rows.append({
                "scenario_id": sid,
                "target": "t",
                "condition": cond,
                "replicate": 0,
                "contradicts_p_explicitly": contra,
                "discloses_adverse_fact": True,
                "confirm_label": "DENY_REPAIR",
                "states_p_explicitly": False,
            })
    df = pd.DataFrame(items_rows)
    judges = pd.DataFrame(judges_rows)
    result = analyze.build_contradiction(df, judges)

    # strong−neutral: every scenario has diff = 0 − 1 = −1, so CI is [−1, −1].
    row_strong = result[result["condition"] == "strong"].iloc[0]
    assert "contradiction_boot_strong_minus_neutral_ci_lo" in result.columns
    lo = row_strong["contradiction_boot_strong_minus_neutral_ci_lo"]
    hi = row_strong["contradiction_boot_strong_minus_neutral_ci_hi"]
    assert np.isclose(lo, -1.0), f"expected lo ≈ −1, got {lo}"
    assert np.isclose(hi, -1.0), f"expected hi ≈ −1, got {hi}"

    # mild−neutral: half the scenarios differ from neutral, so CI has width.
    row_mild = result[result["condition"] == "mild"].iloc[0]
    lo_m = row_mild.get("contradiction_boot_mild_minus_neutral_ci_lo", float("nan"))
    hi_m = row_mild.get("contradiction_boot_mild_minus_neutral_ci_hi", float("nan"))
    if not np.isnan(lo_m):
        assert hi_m > lo_m, f"mild−neutral CI should have width: [{lo_m}, {hi_m}]"

    # stops_correcting_then_denies: strong has contradiction=False + DENY_REPAIR → 6
    assert row_strong["stops_correcting_then_denies"] == 6
