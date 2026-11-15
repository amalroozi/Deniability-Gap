"""Command line entry point: python -m dgap.run"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

from dgap import models, pipeline
from dgap.metrics import write_tables
from dgap.models import MODEL_GROUPS, load_model_config, mock_enabled
from dgap.schema import load_scenarios

STAGE_CHOICES = ("all", *pipeline.STAGES, "metrics")

# Which config groups each stage calls.
STAGE_GROUPS = {
    "targets": ("targets",),
    "beliefs": ("targets",),
    "readers": ("readers",),
    "judges": ("judges",),
    "metrics": (),
}

# litellm provider prefix -> the environment variable holding its API key.
PROVIDER_KEY_ENV = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "groq": "GROQ_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
}

_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


class ConfigError(Exception):
    """The run cannot start as configured."""


def check_no_placeholders(config: dict) -> None:
    bad = [
        f"{group}/{entry['name']}: {entry['model']}"
        for group in MODEL_GROUPS
        for entry in config[group]
        if "FILL_ME" in entry["model"]
    ]
    if bad:
        raise ConfigError(
            "FILL_ME placeholder in config/models.yaml; replace every model string before a "
            "real run (or set DGAP_MOCK=1 for a mock run). No model call was made.\n  "
            + "\n  ".join(bad)
        )


def check_api_keys(config: dict, groups, env) -> None:
    problems = []
    for group in groups:
        for entry in config[group]:
            provider = entry["model"].split("/", 1)[0] if "/" in entry["model"] else None
            variable = PROVIDER_KEY_ENV.get(provider)
            if variable is None:
                problems.append(
                    f"{group}/{entry['name']}: cannot tell which API key {entry['model']!r} needs; "
                    f"use a provider prefix from {sorted(PROVIDER_KEY_ENV)}"
                )
            elif not env.get(variable):
                problems.append(f"{group}/{entry['name']}: {variable} is not set")
    if problems:
        raise ConfigError("API key check failed. No model call was made.\n  " + "\n  ".join(problems))


def planned_calls_by_model(config: dict, *, n_scenarios: int, n_conditions: int, repeats: int) -> dict:
    """Planned calls per stage and model: {stage: [(entry, calls), ...]}."""
    items = n_scenarios * len(config["targets"]) * n_conditions * repeats
    per_target = n_scenarios * n_conditions * repeats
    return {
        "targets": [(entry, 2 * per_target) for entry in config["targets"]],
        "beliefs": [(entry, n_scenarios * repeats) for entry in config["targets"]],
        "readers": [(entry, items) for entry in config["readers"]],
        "judges": [(entry, items) for entry in config["judges"]],
        "metrics": [],
    }


def _print_dry_run(stages, counts, by_model) -> None:
    print("DRY RUN: no calls made. Planned calls with nothing cached or already written:")
    for stage in stages:
        print(f"  {stage:<8} {counts[stage]:>6}")
    print(f"  {'total':<8} {sum(counts[stage] for stage in stages):>6}")
    print("Planned calls per stage and model:")
    totals: dict[str, dict] = {}
    for stage in stages:
        for entry, calls in by_model[stage]:
            print(f"  {stage}: {entry['name']} = {calls} calls")
            total = totals.setdefault(entry["model"], {"names": [], "calls": 0, "rpd": entry.get("rpd")})
            total["calls"] += calls
            if entry["name"] not in total["names"]:
                total["names"].append(entry["name"])
    print("Planned calls per model against its daily request cap (rpd):")
    for model, total in totals.items():
        share = f"{total['calls'] / total['rpd']:.1%} of rpd={total['rpd']}" if total["rpd"] else "no rpd set"
        print(f"  model {'+'.join(total['names'])} [{model}]: {total['calls']} calls, {share}")


def _redact(text: str, env) -> str:
    """Remove any API key value from a message before it is printed."""
    for variable in PROVIDER_KEY_ENV.values():
        value = env.get(variable)
        if value:
            text = text.replace(value, f"<{variable}>")
    return text


def _print_usage(config: dict, runs_dir: Path, mock: bool) -> None:
    """Requests and tokens per model over the last 24 hours, against its caps."""
    used = pipeline.daily_usage(runs_dir, mock=mock)
    print("Usage in the last 24 hours (all runs; cache hits not counted):")
    seen = set()
    for group in MODEL_GROUPS:
        for entry in config[group]:
            if entry["model"] in seen:
                continue
            seen.add(entry["model"])
            usage = used.get(entry["model"], {"requests": 0, "tokens": 0})
            rpd, tpd = entry.get("rpd"), entry.get("tpd")
            requests = f"{usage['requests']}/{rpd} ({usage['requests'] / rpd:.1%})" if rpd else f"{usage['requests']}/no cap"
            tokens = f"{usage['tokens']}/{tpd} ({usage['tokens'] / tpd:.1%})" if tpd else f"{usage['tokens']}/no cap"
            print(f"  usage {entry['name']} [{entry['model']}]: requests {requests}; tokens {tokens}")


def _parse_conditions(text: str) -> list[str]:
    conditions = [part.strip() for part in text.split(",") if part.strip()]
    unknown = [c for c in conditions if c not in pipeline.CONDITIONS]
    if not conditions or unknown or len(set(conditions)) != len(conditions):
        raise argparse.ArgumentTypeError(
            f"expected a comma-separated subset of {','.join(pipeline.CONDITIONS)}, got {text!r}"
        )
    return conditions


def _positive_int(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return value


def _run_id(text: str) -> str:
    if not _RUN_ID.fullmatch(text):
        raise argparse.ArgumentTypeError("use only letters, digits, '.', '_' and '-'")
    return text


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m dgap.run", description="Run the Deniability Gap pipeline.")
    parser.add_argument("--scenarios", required=True, type=Path, help="JSONL file of scenarios")
    parser.add_argument("--run-id", required=True, type=_run_id)
    parser.add_argument("--stage", default="all", choices=STAGE_CHOICES)
    parser.add_argument("--conditions", default=list(pipeline.CONDITIONS), type=_parse_conditions)
    parser.add_argument("--repeats", default=1, type=_positive_int, help="replicates per item")
    parser.add_argument("--limit", default=None, type=_positive_int, help="use only the first N scenarios")
    parser.add_argument(
        "--workers",
        default=1,
        type=_positive_int,
        help="parallel calls in the readers and judges stages (default 1: one at a time)",
    )
    parser.add_argument("--dry-run", action="store_true", help="print call counts and make no calls")
    return parser


def main(argv=None, *, config_path="config/models.yaml", results_dir="results") -> int:
    args = build_parser().parse_args(argv)
    stages = [*pipeline.STAGES, "metrics"] if args.stage == "all" else [args.stage]
    mock = mock_enabled()
    mode = "MOCK" if mock else "REAL"

    scenarios = load_scenarios(args.scenarios)
    if args.limit is not None:
        scenarios = scenarios[: args.limit]
    config = load_model_config(config_path)
    counts = pipeline.planned_calls(
        n_scenarios=len(scenarios),
        n_targets=len(config["targets"]),
        n_readers=len(config["readers"]),
        n_judges=len(config["judges"]),
        n_conditions=len(args.conditions),
        repeats=args.repeats,
    )
    counts["metrics"] = 0

    print(
        f"run_id={args.run_id} mode={mode} scenarios={len(scenarios)} "
        f"targets={len(config['targets'])} readers={len(config['readers'])} "
        f"judges={len(config['judges'])} conditions={','.join(args.conditions)} repeats={args.repeats}"
    )

    if args.dry_run:
        by_model = planned_calls_by_model(
            config, n_scenarios=len(scenarios), n_conditions=len(args.conditions), repeats=args.repeats
        )
        _print_dry_run(stages, counts, by_model)
        return 0

    if not mock:
        try:
            check_no_placeholders(config)
            from dotenv import load_dotenv

            load_dotenv()
            groups = sorted({group for stage in stages for group in STAGE_GROUPS[stage]})
            check_api_keys(config, groups, os.environ)
        except ConfigError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2

    results_dir = Path(results_dir)
    paths = {"runs_dir": results_dir / "raw" / "runs", "cache_dir": results_dir / "raw" / "cache"}
    grid = (args.conditions, args.repeats)
    status = 0
    for stage in stages:
        try:
            if stage == "targets":
                stats = pipeline.run_targets(args.run_id, scenarios, config["targets"], *grid, **paths)
            elif stage == "beliefs":
                stats = pipeline.run_beliefs(args.run_id, scenarios, config["targets"], args.repeats, **paths)
            elif stage == "readers":
                stats = pipeline.run_readers(
                    args.run_id, scenarios, config["targets"], config["readers"], *grid, **paths, workers=args.workers
                )
            elif stage == "judges":
                stats = pipeline.run_judges(
                    args.run_id, scenarios, config["targets"], config["judges"], *grid, **paths, workers=args.workers
                )
            else:
                summary_path, items_path, table_mock = write_tables(
                    paths["runs_dir"] / args.run_id, results_dir / "tables", args.run_id
                )
                print(
                    f"  metrics  wrote {summary_path} and {items_path}" + (" (MOCK numbers)" if table_mock else "")
                )
                continue
        except Exception as exc:
            # A persistent provider error. Records already written are kept.
            message = _redact(f"{type(exc).__name__}: {exc}", os.environ)
            print(
                f"ERROR: stage {stage} failed and the run stopped. Rerun the same command to "
                f"resume.\n  {message}",
                file=sys.stderr,
            )
            status = 1
            break
        line = (
            f"  {stage:<8} planned={counts[stage]} new records={stats['written']} "
            f"(from cache: {stats['cached']}) skipped, already written={stats['skipped']}"
        )
        if stats["no_advice"]:
            line += f" items without usable advice={stats['no_advice']}"
        if stats["failed"]:
            line += f" FAILED calls (recorded; retried on resume)={stats['failed']}"
        print(line)
        if stats["stopped"]:
            print(
                f"STOPPED: stage {stage} stopped on its daily budget; later stages were not run. "
                f"Rerun the same command to resume.\n  {stats['stopped']}"
            )
            status = 3
            break
    if models.rate_limit_events:
        by_model: dict[str, int] = {}
        for event in models.rate_limit_events:
            by_model[event["model"]] = by_model.get(event["model"], 0) + 1
        print("Rate limits (HTTP 429) seen: " + ", ".join(f"{m}={n}" for m, n in sorted(by_model.items())))
    if not mock:
        _print_usage(config, paths["runs_dir"], mock)
    return status

if __name__ == "__main__":
    sys.exit(main())
