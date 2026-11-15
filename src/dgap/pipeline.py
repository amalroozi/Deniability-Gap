"""The four measurement stages.

Each stage appends one JSONL record per model call to
<runs_dir>/<run_id>/<stage>.jsonl and skips any record already in that file,
so an interrupted run can be restarted with the same command.
"""

from __future__ import annotations

import json
import threading
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dgap.models import complete, mock_enabled
from dgap.parse import parse_judge, parse_probability, parse_yes_no
from dgap.prompts import format_facts, render
from dgap.schema import Scenario

CONDITIONS = ("neutral", "mild", "strong")
STAGES = ("targets", "beliefs", "readers", "judges")

# Targets and beliefs are sampled, one draw per replicate (seed = replicate).
# Readers and judges are measuring instruments and run once, deterministically.
TARGET_TEMPERATURE = 1.0
BELIEF_TEMPERATURE = 1.0
READER_TEMPERATURE = 0.0
JUDGE_TEMPERATURE = 0.0
INSTRUMENT_SEED = 0

# Default output token caps by record kind.
MAX_TOKENS = {
    "advice": 1200,
    "confirm": 250,
    "belief": 300,
    "reader": 300,
    "judge": 300,
}

# A stage stops before a call that would take a model past this share of its
# daily request cap (rpd) or daily token cap (tpd).
BUDGET_FRACTION = 0.9
BUDGET_WINDOW = timedelta(hours=24)

DEFAULT_RUNS_DIR = "results/raw/runs"
DEFAULT_CACHE_DIR = "results/raw/cache"


# ---------------------------------------------------------------- prompts


def build_system_prompt(scenario: Scenario, condition: str) -> str:
    values = {"advisor_role": scenario.advisor_role, "facts": format_facts(scenario.facts, scenario.id)}
    if condition == "neutral":
        return render("system_neutral", **values)
    if condition == "mild":
        return render("system_mild", incentive_context=scenario.incentive_context, **values)
    if condition == "strong":
        return render(
            "system_strong",
            incentive_context=scenario.incentive_context,
            incentive_goal=scenario.incentive_goal,
            **values,
        )
    raise ValueError(f"unknown condition {condition!r}; expected one of {CONDITIONS}")


def build_advice_messages(scenario: Scenario, condition: str) -> list[dict]:
    return [
        {"role": "system", "content": build_system_prompt(scenario, condition)},
        {"role": "user", "content": render("advice_user", user_query=scenario.user_query)},
    ]


def build_confirm_messages(scenario: Scenario, condition: str, advice: str) -> list[dict]:
    return build_advice_messages(scenario, condition) + [
        {"role": "assistant", "content": advice},
        {"role": "user", "content": render("confirm_probe", proposition=scenario.proposition)},
    ]


def build_belief_messages(scenario: Scenario) -> list[dict]:
    content = render(
        "belief_probe", facts=format_facts(scenario.facts, scenario.id), proposition=scenario.proposition
    )
    return [{"role": "user", "content": content}]


def build_reader_messages(message: str, proposition: str) -> list[dict]:
    """The reader sees the advice message and the proposition, nothing else."""
    return [
        {"role": "user", "content": render("reader_probe", message=message, proposition=proposition)}
    ]


def build_judge_messages(message: str, reply: str, proposition: str) -> list[dict]:
    """One judge call per item: the advice message and the confirm reply."""
    content = render("judge", message=message, reply=reply, proposition=proposition)
    return [{"role": "user", "content": content}]


# ---------------------------------------------------------------- records


def read_jsonl(path: str | Path) -> list[dict]:
    path = Path(path)
    if not path.is_file():
        return []
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def record_key(record: dict) -> tuple:
    return (
        record["kind"],
        record["scenario_id"],
        record["target"],
        record["condition"],
        record["replicate"],
        record["model"],
    )


# ----------------------------------------------------------------- budget


class BudgetExceeded(Exception):
    """The next call would take a model past its daily budget."""


def usage_tokens(usage: dict | None) -> int:
    """Total tokens a call used, reasoning tokens included.

    Providers count reasoning inside completion_tokens or total_tokens, so
    the larger of total_tokens and prompt + completion is used.
    """
    if not isinstance(usage, dict):
        return 0

    def number(key):
        value = usage.get(key)
        return value if isinstance(value, (int, float)) and not isinstance(value, bool) else 0

    return int(max(number("total_tokens"), number("prompt_tokens") + number("completion_tokens")))


def estimate_tokens(messages: list[dict], max_tokens: int | None) -> int:
    """A deliberately high guess at what one call will use."""
    characters = sum(len(str(message.get("content", ""))) for message in messages)
    return characters // 3 + (max_tokens or 0)


def daily_usage(runs_dir: str | Path, *, mock: bool, now: datetime | None = None) -> dict[str, dict[str, int]]:
    """Requests and tokens per model string over the last 24 hours.

    Counted from the records of every run under `runs_dir`. Records served
    from the cache made no request and are not counted. A rolling 24 hours is
    used rather than a calendar day, so it can only over-count against a
    provider whose quota resets at its own midnight.
    """
    now = now or datetime.now(timezone.utc)
    used: dict[str, dict[str, int]] = {}
    runs_dir = Path(runs_dir)
    if not runs_dir.is_dir():
        return used
    for path in sorted(runs_dir.glob("*/*.jsonl")):
        for record in read_jsonl(path):
            if record.get("mock") != mock or record.get("cached"):
                continue
            try:
                when = datetime.fromisoformat(record["timestamp"])
            except (KeyError, TypeError, ValueError):
                continue
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            if now - when >= BUDGET_WINDOW:
                continue
            entry = used.setdefault(record.get("model_string", ""), {"requests": 0, "tokens": 0})
            entry["requests"] += 1
            entry["tokens"] += usage_tokens(record.get("usage"))
    return used


class Budget:
    """Tracks each model's requests and tokens against 90% of rpd and tpd.

    Thread-safe. `check` reserves one request and the token estimate for the
    call it lets through; `add` swaps that reservation for the real usage. So
    calls in flight on other threads count against the cap, and parallel
    calls can never pass it together.
    """

    def __init__(self, runs_dir: str | Path, *, mock: bool):
        self.used = daily_usage(runs_dir, mock=mock)
        self.reserved: dict[str, dict[str, int]] = {}
        self._lock = threading.Lock()

    def check(self, entry: dict, messages: list[dict], max_tokens: int | None) -> int:
        """Raise BudgetExceeded if one more call would pass 90% of a cap.

        Otherwise reserve the call and return its token estimate, to be
        passed back to `add`.
        """
        with self._lock:
            return self._check(entry, messages, max_tokens)

    def _check(self, entry: dict, messages: list[dict], max_tokens: int | None) -> int:
        done = self.used.get(entry["model"], {"requests": 0, "tokens": 0})
        pending = self.reserved.get(entry["model"], {"requests": 0, "tokens": 0})
        used = {name: done[name] + pending[name] for name in ("requests", "tokens")}
        rpd, tpd = entry.get("rpd"), entry.get("tpd")
        if rpd and used["requests"] + 1 > BUDGET_FRACTION * rpd:
            raise BudgetExceeded(
                f"daily request budget reached for {entry['name']} ({entry['model']}): "
                f"{used['requests']} requests in the last 24 hours, and one more would pass "
                f"{BUDGET_FRACTION:.0%} of rpd={rpd}"
            )
        estimate = estimate_tokens(messages, max_tokens)
        if tpd and used["tokens"] + estimate > BUDGET_FRACTION * tpd:
            raise BudgetExceeded(
                f"daily token budget reached for {entry['name']} ({entry['model']}): "
                f"{used['tokens']} tokens in the last 24 hours, and a call of about {estimate} "
                f"more would pass {BUDGET_FRACTION:.0%} of tpd={tpd}"
            )
        pending = self.reserved.setdefault(entry["model"], {"requests": 0, "tokens": 0})
        pending["requests"] += 1
        pending["tokens"] += estimate
        return estimate

    def add(self, entry: dict, usage: dict | None, reserved_tokens: int | None = None) -> None:
        """Count a finished request, releasing its reservation if it had one."""
        with self._lock:
            if reserved_tokens is not None:
                pending = self.reserved[entry["model"]]
                pending["requests"] -= 1
                pending["tokens"] -= reserved_tokens
            used = self.used.setdefault(entry["model"], {"requests": 0, "tokens": 0})
            used["requests"] += 1
            used["tokens"] += usage_tokens(usage)


class _StageFile:
    """One stage's JSONL file: what is already in it, and appends to it.

    With `tolerate_failures`, a call that still raises after its retries
    (other than a budget stop) is written as a record with "failed": true,
    parse_ok false and the error type, and the stage carries on. Failed
    records are not treated as done: a resumed run calls them again, and the
    new record replaces the failed one in the file. Without it, the error
    stops the stage.

    `call` is thread-safe: the model call runs unlocked, while the skip check,
    the file write and the stats update happen under one lock, so records are
    never duplicated or interleaved.
    """

    def __init__(
        self, runs_dir: str | Path, run_id: str, stage: str, cache_dir: str | Path, *, tolerate_failures: bool = False
    ):
        self.tolerate_failures = tolerate_failures
        self.run_id = run_id
        self.stage = stage
        self.cache_dir = cache_dir
        self.mock = mock_enabled()
        self.path = Path(runs_dir) / run_id / f"{stage}.jsonl"
        existing = read_jsonl(self.path)
        if any(record["mock"] != self.mock for record in existing):
            raise RuntimeError(
                f"{self.path} holds {'real' if self.mock else 'MOCK'} records but this is a "
                f"{'MOCK' if self.mock else 'real'} run. Mock and real records must never share "
                "a run; use a different --run-id."
            )
        self.records = {record_key(r): r for r in existing if not r.get("failed")}
        # Failed calls to retry; each is dropped from the file when its key is written again.
        self.failed = {record_key(r) for r in existing if r.get("failed")} - self.records.keys()
        self.budget = Budget(runs_dir, mock=self.mock)
        self.stats = {"written": 0, "skipped": 0, "cached": 0, "no_advice": 0, "stopped": None, "failed": 0}
        self._lock = threading.Lock()
        self._in_flight: set[tuple] = set()

    def call(
        self,
        *,
        kind: str,
        scenario_id: str,
        entry: dict,
        target: str,
        condition: str | None,
        replicate: int,
        messages: list[dict],
        temperature: float,
        seed: int,
        mock_kind: str,
        parser,
    ) -> dict:
        """Return the record for this call, making the call only if it is new."""
        key = (kind, scenario_id, target, condition, replicate, entry["name"])
        with self._lock:
            if key in self.records:
                self.stats["skipped"] += 1
                return self.records[key]
            if key in self._in_flight:
                raise RuntimeError(f"call {key} was submitted twice in one stage")
            self._in_flight.add(key)
        try:
            return self._call(key, kind=kind, scenario_id=scenario_id, entry=entry, target=target,
                              condition=condition, replicate=replicate, messages=messages,
                              temperature=temperature, seed=seed, mock_kind=mock_kind, parser=parser)
        finally:
            with self._lock:
                self._in_flight.discard(key)

    def _call(self, key, *, kind, scenario_id, entry, target, condition, replicate, messages,
              temperature, seed, mock_kind, parser) -> dict:
        # A model's own cap (config max_tokens) replaces the default for its kind.
        max_tokens = entry.get("max_tokens") or MAX_TOKENS[kind]
        # Token estimate reserved in the budget, set once a request is let through.
        reservation: list[int] = []
        error = None
        try:
            result = complete(
                entry["model"],
                messages,
                temperature=temperature,
                seed=seed,
                cache_dir=self.cache_dir,
                mock_kind=mock_kind,
                max_tokens=max_tokens,
                reasoning_effort=entry.get("reasoning_effort"),
                rpm=entry.get("rpm"),
                # Runs only when a request is about to be made, never on a cache hit.
                before_call=lambda: reservation.append(self.budget.check(entry, messages, max_tokens)),
            )
        except BudgetExceeded:
            raise
        except Exception as exc:
            if reservation:
                # The request was made and failed: it still counts, as before.
                self.budget.add(entry, {}, reservation.pop())
            if not self.tolerate_failures:
                raise
            error = type(exc).__name__
            result = {"text": "", "usage": {}, "cached": False,
                      "timestamp": datetime.now(timezone.utc).isoformat()}
        if reservation:
            self.budget.add(entry, result["usage"], reservation.pop())
        parsed = None if error else parser(result["text"])
        record = {
            "run_id": self.run_id,
            "stage": self.stage,
            "kind": kind,
            "scenario_id": scenario_id,
            "model": entry["name"],
            "model_string": entry["model"],
            "target": target,
            "condition": condition,
            "replicate": replicate,
            "temperature": temperature,
            "seed": seed,
            "max_tokens": max_tokens,
            "reasoning_effort": entry.get("reasoning_effort"),
            "messages": messages,
            "response": result["text"],
            "parsed": parsed,
            "parse_ok": parsed is not None,
            "usage": result["usage"],
            "reasoning_tokens": result["usage"].get("reasoning_tokens"),
            "cached": result["cached"],
            "mock": self.mock,
            "timestamp": result["timestamp"],
        }
        if error:
            record["failed"] = True
            record["error"] = error
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if key in self.failed:
                # Replace the earlier failed record for this call.
                kept = [r for r in read_jsonl(self.path) if record_key(r) != key]
                tmp = self.path.with_name(self.path.name + ".tmp")
                tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in kept), encoding="utf-8")
                tmp.replace(self.path)
                self.failed.discard(key)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            if error:
                self.failed.add(key)
                self.stats["failed"] += 1
            else:
                self.records[key] = record
            self.stats["written"] += 1
            self.stats["cached"] += int(result["cached"])
        return record


def _run_calls(stage: _StageFile, calls, workers: int = 1) -> None:
    """Make every call in `calls` (an iterable of `stage.call` keyword dicts).

    With workers=1 the calls run one after another, in order. With more, up
    to `workers` run at once on a thread pool; `calls` is consumed lazily, so
    no more than `workers` calls are ever queued. If a call raises (a budget
    stop, or an error the stage does not tolerate), no further calls are
    started, the ones in flight finish and are written, and the first error
    is raised (a budget stop first, so the stage reports it as a stop).
    """
    if workers <= 1:
        for kwargs in calls:
            stage.call(**kwargs)
        return
    errors: list[BaseException] = []

    def collect(done):
        for future in done:
            exc = future.exception()
            if exc is not None:
                errors.append(exc)

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix=f"dgap-{stage.stage}") as pool:
        pending: set = set()
        try:
            for kwargs in calls:
                if errors:
                    break
                pending.add(pool.submit(stage.call, **kwargs))
                if len(pending) >= workers:
                    done, pending = wait(pending, return_when=FIRST_COMPLETED)
                    collect(done)
        finally:
            done, _ = wait(pending)
            collect(done)
    if errors:
        budget = [exc for exc in errors if isinstance(exc, BudgetExceeded)]
        raise (budget or errors)[0]


def _parse_advice(text: str) -> str | None:
    return text.strip() or None


def _items(scenarios, targets, conditions, repeats):
    for scenario in scenarios:
        for target in targets:
            for condition in conditions:
                for replicate in range(repeats):
                    yield scenario, target, condition, replicate


def _load_target_records(runs_dir: str | Path, run_id: str) -> dict[tuple, dict]:
    path = Path(runs_dir) / run_id / "targets.jsonl"
    return {record_key(record): record for record in read_jsonl(path)}


def _advice_for(target_records, runs_dir, run_id, scenario, target, condition, replicate) -> dict:
    key = ("advice", scenario.id, target["name"], condition, replicate, target["name"])
    if key not in target_records:
        raise RuntimeError(
            f"no advice record for scenario={scenario.id} target={target['name']} "
            f"condition={condition} replicate={replicate} in {Path(runs_dir) / run_id}. "
            "Run the targets stage with the same scenarios, conditions and repeats first."
        )
    return target_records[key]


def planned_calls(
    *, n_scenarios: int, n_targets: int, n_readers: int, n_judges: int, n_conditions: int, repeats: int
) -> dict[str, int]:
    """Model calls each stage makes for a full grid with nothing cached."""
    items = n_scenarios * n_targets * n_conditions * repeats
    return {
        "targets": 2 * items,
        "beliefs": n_scenarios * n_targets * repeats,
        "readers": items * n_readers,
        "judges": items * n_judges,
    }


# ----------------------------------------------------------------- stages


def run_targets(
    run_id, scenarios, targets, conditions, repeats, *, runs_dir=DEFAULT_RUNS_DIR, cache_dir=DEFAULT_CACHE_DIR
) -> dict:
    """Advice turn, then the confirm probe in the same conversation."""
    stage = _StageFile(runs_dir, run_id, "targets", cache_dir)
    try:
        for scenario, target, condition, replicate in _items(scenarios, targets, conditions, repeats):
            common = dict(
                scenario_id=scenario.id,
                entry=target,
                target=target["name"],
                condition=condition,
                replicate=replicate,
                temperature=TARGET_TEMPERATURE,
                seed=replicate,
            )
            advice = stage.call(
                kind="advice",
                messages=build_advice_messages(scenario, condition),
                mock_kind="advice",
                parser=_parse_advice,
                **common,
            )
            if not advice["parse_ok"]:
                # An empty advice turn cannot be continued, rated or judged.
                stage.stats["no_advice"] += 1
                continue
            stage.call(
                kind="confirm",
                messages=build_confirm_messages(scenario, condition, advice["response"]),
                mock_kind="confirm",
                parser=parse_yes_no,
                **common,
            )
    except BudgetExceeded as exc:
        # Everything written so far is kept; rerunning the command resumes here.
        stage.stats["stopped"] = str(exc)
    return stage.stats


def run_beliefs(
    run_id, scenarios, targets, repeats, *, runs_dir=DEFAULT_RUNS_DIR, cache_dir=DEFAULT_CACHE_DIR
) -> dict:
    """Fresh context: the target sees the facts and rates the proposition."""
    stage = _StageFile(runs_dir, run_id, "beliefs", cache_dir)
    try:
        for scenario in scenarios:
            for target in targets:
                for replicate in range(repeats):
                    stage.call(
                        kind="belief",
                        scenario_id=scenario.id,
                        entry=target,
                        target=target["name"],
                        condition=None,
                        replicate=replicate,
                        messages=build_belief_messages(scenario),
                        temperature=BELIEF_TEMPERATURE,
                        seed=replicate,
                        mock_kind="probability",
                        parser=parse_probability,
                    )
    except BudgetExceeded as exc:
        # Everything written so far is kept; rerunning the command resumes here.
        stage.stats["stopped"] = str(exc)
    return stage.stats


def run_readers(
    run_id,
    scenarios,
    targets,
    readers,
    conditions,
    repeats,
    *,
    runs_dir=DEFAULT_RUNS_DIR,
    cache_dir=DEFAULT_CACHE_DIR,
    workers=1,
) -> dict:
    """Every reader rates every advice message, seeing only the message.

    `workers` > 1 runs up to that many reader calls at once.
    """
    stage = _StageFile(runs_dir, run_id, "readers", cache_dir, tolerate_failures=True)
    target_records = _load_target_records(runs_dir, run_id)

    def calls():
        for scenario, target, condition, replicate in _items(scenarios, targets, conditions, repeats):
            advice = _advice_for(target_records, runs_dir, run_id, scenario, target, condition, replicate)
            if not advice["parse_ok"]:
                stage.stats["no_advice"] += 1
                continue
            for reader in readers:
                yield dict(
                    kind="reader",
                    scenario_id=scenario.id,
                    entry=reader,
                    target=target["name"],
                    condition=condition,
                    replicate=replicate,
                    messages=build_reader_messages(advice["response"], scenario.proposition),
                    temperature=READER_TEMPERATURE,
                    seed=INSTRUMENT_SEED,
                    mock_kind="probability",
                    parser=parse_probability,
                )

    try:
        _run_calls(stage, calls(), workers)
    except BudgetExceeded as exc:
        # Everything written so far is kept; rerunning the command resumes here.
        stage.stats["stopped"] = str(exc)
    return stage.stats


def run_judges(
    run_id,
    scenarios,
    targets,
    judges,
    conditions,
    repeats,
    *,
    runs_dir=DEFAULT_RUNS_DIR,
    cache_dir=DEFAULT_CACHE_DIR,
    workers=1,
) -> dict:
    """One judge call per item, on its advice message and confirm reply together.

    `workers` > 1 runs up to that many judge calls at once.
    """
    stage = _StageFile(runs_dir, run_id, "judges", cache_dir, tolerate_failures=True)
    target_records = _load_target_records(runs_dir, run_id)

    def calls():
        for scenario, target, condition, replicate in _items(scenarios, targets, conditions, repeats):
            advice = _advice_for(target_records, runs_dir, run_id, scenario, target, condition, replicate)
            if not advice["parse_ok"]:
                stage.stats["no_advice"] += 1
                continue
            confirm = target_records.get(
                ("confirm", scenario.id, target["name"], condition, replicate, target["name"])
            )
            if confirm is None:
                raise RuntimeError(
                    f"no confirm record for scenario={scenario.id} target={target['name']} "
                    f"condition={condition} replicate={replicate}. Finish the targets stage first."
                )
            for judge in judges:
                yield dict(
                    kind="judge",
                    scenario_id=scenario.id,
                    entry=judge,
                    target=target["name"],
                    condition=condition,
                    replicate=replicate,
                    messages=build_judge_messages(advice["response"], confirm["response"], scenario.proposition),
                    temperature=JUDGE_TEMPERATURE,
                    seed=INSTRUMENT_SEED,
                    mock_kind="judge",
                    parser=parse_judge,
                )

    try:
        _run_calls(stage, calls(), workers)
    except BudgetExceeded as exc:
        # Everything written so far is kept; rerunning the command resumes here.
        stage.stats["stopped"] = str(exc)
    return stage.stats
