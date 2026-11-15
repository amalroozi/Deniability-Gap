"""Sample a blind human-labelling sheet from results/raw/runs/main/targets.jsonl.

Usage: python scripts/make_label_sheet.py
"""
import json
from pathlib import Path

import numpy as np

SEED = 0
N_ITEMS = 20

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TARGETS_PATH = PROJECT_ROOT / "results" / "raw" / "runs" / "main" / "targets.jsonl"
SCENARIOS_PATH = PROJECT_ROOT / "data" / "scenarios.jsonl"
LABELS_PATH = PROJECT_ROOT / "results" / "human_labels.csv"
KEY_PATH = PROJECT_ROOT / "results" / "human_labels_key.csv"


def load_jsonl(path: Path) -> list[dict]:
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def main():
    scenarios = {s["id"]: s for s in load_jsonl(SCENARIOS_PATH)}
    records = [r for r in load_jsonl(TARGETS_PATH) if r["kind"] == "advice"]

    cells: dict[tuple[str, str], list[dict]] = {}
    for r in records:
        cells.setdefault((r["target"], r["condition"]), []).append(r)
    cell_keys = sorted(cells.keys())

    rng = np.random.default_rng(SEED)

    base = N_ITEMS // len(cell_keys)
    remainder = N_ITEMS % len(cell_keys)
    extra_cells = set(
        int(i) for i in rng.choice(len(cell_keys), size=remainder, replace=False)
    )

    selected = []
    for i, key in enumerate(cell_keys):
        quota = base + (1 if i in extra_cells else 0)
        pool = cells[key]
        idx = rng.choice(len(pool), size=min(quota, len(pool)), replace=False)
        selected.extend(pool[j] for j in idx)

    order = rng.permutation(len(selected))
    selected = [selected[i] for i in order]

    with open(LABELS_PATH, "w", newline="") as f_labels, open(KEY_PATH, "w", newline="") as f_key:
        import csv

        labels_writer = csv.writer(f_labels)
        labels_writer.writerow(["item_key", "proposition", "advice_text", "human_probability"])

        key_writer = csv.writer(f_key)
        key_writer.writerow(["item_key", "scenario_id", "target", "condition", "replicate", "proposition"])

        for n, r in enumerate(selected, start=1):
            item_key = f"item{n:02d}"
            proposition = scenarios[r["scenario_id"]]["proposition"]
            labels_writer.writerow([item_key, proposition, r["response"], ""])
            key_writer.writerow([item_key, r["scenario_id"], r["target"], r["condition"], r["replicate"], proposition])

    print(f"Wrote {len(selected)} items to {LABELS_PATH} and {KEY_PATH}")


if __name__ == "__main__":
    main()
