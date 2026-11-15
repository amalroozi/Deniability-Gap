"""Cached, retrying model client and model config loader."""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

MODEL_GROUPS = ("targets", "readers", "judges")
MAX_ATTEMPTS = 4
BACKOFF_BASE_SECONDS = 1.0

# Rate limits (HTTP 429) are retried separately from other errors.
RATE_LIMIT_MAX_ATTEMPTS = 6
RATE_LIMIT_BACKOFF_BASE_SECONDS = 5.0
RATE_LIMIT_BACKOFF_CAP_SECONDS = 60.0
# A provider that asks for a longer wait than this is out of quota for now
# (for example a daily cap): the call fails instead of sleeping through it.
RETRY_AFTER_MAX_SECONDS = 120.0
# Calls are spaced a little wider than 60 / rpm so the limit is never touched.
PACING_MARGIN = 1.05

# Optional per-model fields in config/models.yaml.
LIMIT_FIELDS = ("rpm", "rpd", "tpd")
INT_FIELDS = (*LIMIT_FIELDS, "max_tokens")
OPTIONAL_FIELDS = (*INT_FIELDS, "reasoning_effort")

# One entry per HTTP 429 seen in this process: {"model", "attempt", "wait"}.
rate_limit_events: list[dict] = []
# model string -> time.monotonic() of the last real call attempt.
_last_call: dict[str, float] = {}

# Errors that a retry cannot fix (bad key, bad model name, bad request).
_NON_RETRYABLE = (
    "AuthenticationError",
    "BadRequestError",
    "NotFoundError",
    "PermissionDeniedError",
)


def _cache_key(
    model: str,
    messages: list[dict],
    temperature: float,
    seed: int,
    max_tokens: int | None = None,
    reasoning_effort: str | None = None,
) -> str:
    fields: dict[str, Any] = {"model": model, "messages": messages, "temperature": temperature, "seed": seed}
    # Only present when set, so a call that sets neither keeps its old key.
    if max_tokens is not None:
        fields["max_tokens"] = max_tokens
    if reasoning_effort is not None:
        fields["reasoning_effort"] = reasoning_effort
    payload = json.dumps(fields, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


_THINK_BLOCK = re.compile(r"<think\b[^>]*>.*?</think\s*>", re.DOTALL | re.IGNORECASE)
_THINK_OPEN = re.compile(r"<think\b[^>]*>.*", re.DOTALL | re.IGNORECASE)


def strip_reasoning(text: str) -> str:
    """Remove <think>...</think> blocks, leaving only the final answer.

    A <think> that is never closed (the output was cut off while the model
    was still reasoning) is removed through to the end, so reasoning text can
    never be stored as the answer. Text before an unmatched </think> is
    reasoning too (some providers drop the opening tag) and is removed.
    """
    text = _THINK_BLOCK.sub("", text)
    text = _THINK_OPEN.sub("", text)
    lowered = text.lower()
    if "</think>" in lowered:
        text = text[lowered.rindex("</think>") + len("</think>") :]
    return text.strip()


def mock_enabled() -> bool:
    return os.environ.get("DGAP_MOCK") == "1"


MOCK_KINDS = ("advice", "confirm", "probability", "judge")
_MOCK_ADVICE = (
    "On balance this looks like a reasonable option for what you describe.",
    "There are points for and against this, so it depends on your priorities.",
    "I would be cautious here and look at the alternatives first.",
)
_MOCK_LABELS = ("AFFIRM", "DENY", "DENY_REPAIR", "EVADE")


def _mock_text(key: str, mock_kind: str | None) -> str:
    """Deterministic placeholder output that varies with the cache key."""
    number = int(key[:8], 16)
    tag = key[:12]
    if mock_kind is None:
        return f"MOCK RESPONSE {tag}"
    if mock_kind == "advice":
        return f"MOCK ADVICE {tag}. {_MOCK_ADVICE[number % len(_MOCK_ADVICE)]} This is placeholder text, not model output."
    if mock_kind == "confirm":
        return f"{'YES' if number % 2 == 0 else 'NO'}. MOCK CONFIRM {tag}."
    if mock_kind == "probability":
        return json.dumps({"probability": number % 101, "reason": f"MOCK REASON {tag}"})
    # "judge": one object that satisfies both judge schemas.
    return json.dumps(
        {
            "states_p_explicitly": bool(number & 1),
            "contradicts_p_explicitly": bool(number & 2),
            "discloses_adverse_fact": bool(number & 4),
            "label": _MOCK_LABELS[(number >> 3) % len(_MOCK_LABELS)],
        }
    )


_original_getaddrinfo = None


def prefer_ipv4() -> None:
    """Make outbound connections try IPv4 addresses first.

    On some networks IPv6 routes to Google's API hosts are broken and a
    connection attempt hangs instead of failing. Putting IPv4 results first
    avoids that; IPv6 is still used if a host has no IPv4 address. Safe to
    call more than once.
    """
    import socket

    global _original_getaddrinfo
    if _original_getaddrinfo is not None:
        return
    _original_getaddrinfo = socket.getaddrinfo

    def getaddrinfo(*args, **kwargs):
        results = _original_getaddrinfo(*args, **kwargs)
        return sorted(results, key=lambda result: result[0] != socket.AF_INET)

    socket.getaddrinfo = getaddrinfo


def _reasoning_tokens(usage: dict) -> int | None:
    """Reasoning token count from a provider usage dict, if it reports one."""
    details = usage.get("completion_tokens_details")
    value = details.get("reasoning_tokens") if isinstance(details, dict) else None
    if value is None:
        value = usage.get("reasoning_tokens")
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _call_litellm(model: str, messages: list[dict], temperature: float, seed: int, **options) -> tuple[str, dict]:
    """Make one real provider call. Returns (text, usage).

    `options` may hold max_tokens and reasoning_effort. The text is the final
    message content only; reasoning_content is never read. `usage` is the
    provider's usage plus "finish_reason" and, when reported, a top-level
    "reasoning_tokens".
    """
    import litellm
    from dotenv import load_dotenv

    load_dotenv()
    prefer_ipv4()
    extra: dict[str, Any] = {}
    if options.get("max_tokens") is not None:
        extra["max_tokens"] = options["max_tokens"]
    effort = options.get("reasoning_effort")
    if effort is not None:
        provider, _, model_id = model.partition("/")
        try:
            supported = litellm.get_supported_openai_params(model=model_id, custom_llm_provider=provider) or []
        except Exception:
            supported = []
        if "reasoning_effort" in supported:
            extra["reasoning_effort"] = effort
        else:
            # litellm would silently drop it (drop_params); send it in the body.
            extra["extra_body"] = {"reasoning_effort": effort}
    response = litellm.completion(
        model=model,
        messages=messages,
        temperature=temperature,
        seed=seed,
        # Some providers reject `seed`; drop it for them instead of failing.
        drop_params=True,
        **extra,
    )
    choice = response.choices[0]
    text = choice.message.content or ""
    usage = getattr(response, "usage", None)
    if usage is None:
        usage_dict: dict[str, Any] = {}
    elif hasattr(usage, "model_dump"):
        usage_dict = usage.model_dump()
    else:
        usage_dict = dict(usage)
    # Round-trip through JSON so the record is always serialisable.
    usage_dict = json.loads(json.dumps(usage_dict, default=str))
    usage_dict["finish_reason"] = getattr(choice, "finish_reason", None)
    reasoning = _reasoning_tokens(usage_dict)
    if reasoning is not None:
        usage_dict["reasoning_tokens"] = reasoning
    return text, usage_dict


def is_rate_limit(exc: BaseException) -> bool:
    """True for an HTTP 429 from the provider."""
    return type(exc).__name__ == "RateLimitError" or getattr(exc, "status_code", None) == 429


_DELAY_IN_MESSAGE = re.compile(
    r"(?:try again in|retry in|retryDelay\W{1,4})\s*(?:(\d+)m)?(\d+(?:\.\d+)?)s", re.IGNORECASE
)


def retry_after_seconds(exc: BaseException) -> float | None:
    """The wait the provider asked for: the Retry-After header, else a delay
    stated in the error message ("try again in 7.5s", "retryDelay": "23s")."""
    for holder in (exc, getattr(exc, "response", None)):
        headers = getattr(holder, "headers", None)
        if headers is None:
            continue
        try:
            value = headers.get("retry-after") or headers.get("Retry-After")
        except Exception:
            value = None
        if value is not None:
            try:
                return max(0.0, float(value))
            except (TypeError, ValueError):
                pass
    match = _DELAY_IN_MESSAGE.search(str(exc))
    if match:
        return 60.0 * int(match.group(1) or 0) + float(match.group(2))
    return None


def _pace(model: str, rpm: int | None) -> None:
    """Sleep so that calls to `model` are never closer than 60 / rpm seconds."""
    if rpm:
        interval = 60.0 / rpm * PACING_MARGIN
        last = _last_call.get(model)
        if last is not None:
            wait = last + interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
    _last_call[model] = time.monotonic()


def _call_with_retries(
    model: str, messages: list[dict], temperature: float, seed: int, rpm: int | None = None, **options
) -> tuple[str, dict]:
    """Call the provider, pacing every attempt and retrying failures.

    A 429 honours Retry-After when given, else backs off (capped at 60 s),
    for up to RATE_LIMIT_MAX_ATTEMPTS attempts. Other retryable errors get up
    to MAX_ATTEMPTS attempts. The last error is raised; nothing is returned
    (and so nothing is cached) for a failed call.
    """
    errors = 0
    rate_limited = 0
    while True:
        _pace(model, rpm)
        try:
            return _call_litellm(model, messages, temperature, seed, **options)
        except Exception as exc:
            if is_rate_limit(exc):
                rate_limited += 1
                wait = retry_after_seconds(exc)
                event = {"model": model, "attempt": rate_limited, "wait": wait}
                rate_limit_events.append(event)
                if rate_limited >= RATE_LIMIT_MAX_ATTEMPTS:
                    raise
                if wait is None:
                    wait = min(
                        RATE_LIMIT_BACKOFF_CAP_SECONDS,
                        RATE_LIMIT_BACKOFF_BASE_SECONDS * 2 ** (rate_limited - 1),
                    )
                    event["wait"] = wait
                elif wait > RETRY_AFTER_MAX_SECONDS:
                    raise
                time.sleep(wait)
                continue
            errors += 1
            if type(exc).__name__ in _NON_RETRYABLE or errors >= MAX_ATTEMPTS:
                raise
            time.sleep(BACKOFF_BASE_SECONDS * 2 ** (errors - 1))


def complete(
    model: str,
    messages: list[dict],
    *,
    temperature: float,
    seed: int,
    cache_dir: str | Path = "results/raw/cache",
    mock_kind: str | None = None,
    max_tokens: int | None = None,
    reasoning_effort: str | None = None,
    rpm: int | None = None,
    before_call=None,
) -> dict:
    """Return a completion as {text, model, cached, usage, timestamp}.

    Responses are cached on disk under the SHA-256 of
    (model, messages, temperature, seed); a cache hit makes no network call.
    With DGAP_MOCK=1 a deterministic canned response is returned and the
    network is never touched. Mock responses are cached in a separate
    `mock/` subdirectory so they can never be served to a real run.

    `mock_kind` shapes the mock output for the role being played: "advice"
    (short prose), "confirm" (starts with YES or NO), "probability" (JSON
    {"probability": int, "reason": str}) or "judge" (JSON carrying the keys
    of both judge schemas). It has no effect on a real call.

    `max_tokens` caps the output and `reasoning_effort` is passed to models
    that reason; both are part of the cache key when set. `rpm` paces real
    calls to this model. `before_call`, if given, is called with no arguments
    just before a response would be generated (never on a cache hit); it may
    raise to refuse the call.

    `text` is the final answer only: any <think>...</think> block is removed
    and the number of characters removed is stored in
    usage["think_chars_stripped"]. A failed call raises and is never cached.
    """
    if mock_kind is not None and mock_kind not in MOCK_KINDS:
        raise ValueError(f"mock_kind must be one of {MOCK_KINDS}, got {mock_kind!r}")
    mock = mock_enabled()
    key = _cache_key(model, messages, temperature, seed, max_tokens, reasoning_effort)
    directory = Path(cache_dir) / "mock" if mock else Path(cache_dir)
    suffix = f".{mock_kind}" if mock and mock_kind else ""
    cache_path = directory / f"{key}{suffix}.json"

    if cache_path.is_file():
        record = json.loads(cache_path.read_text(encoding="utf-8"))
        record["cached"] = True
        return record

    if before_call is not None:
        before_call()

    if mock:
        text, usage = _mock_text(key, mock_kind), {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        }
    else:
        options = {
            name: value
            for name, value in (("max_tokens", max_tokens), ("reasoning_effort", reasoning_effort))
            if value is not None
        }
        text, usage = _call_with_retries(model, messages, temperature, seed, rpm=rpm, **options)
        usage = dict(usage)
        answer = strip_reasoning(text)
        if answer != text.strip():
            usage["think_chars_stripped"] = len(text) - len(answer)
        text = answer

    record = {
        "text": text,
        "model": model,
        "cached": False,
        "usage": usage,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    directory.mkdir(parents=True, exist_ok=True)
    tmp_path = cache_path.with_name(f"{cache_path.name}.{os.getpid()}.tmp")
    tmp_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp_path, cache_path)
    return record


def load_model_config(path: str | Path = "config/models.yaml") -> dict[str, list[dict[str, Any]]]:
    """Load config/models.yaml as {"targets": [...], "readers": [...], "judges": [...]}.

    Each entry has "name" (short label) and "model" (litellm model string),
    plus the optional fields, which are None when not given: "rpm" (requests
    per minute), "rpd" (requests per day), "tpd" (tokens per day),
    "max_tokens" (output cap for this model, replacing the per-kind default)
    and "reasoning_effort" (string passed to models that reason).
    """
    path = Path(path)
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a mapping with 'targets', 'readers' and 'judges'")
    config: dict[str, list[dict[str, Any]]] = {}
    for group in MODEL_GROUPS:
        entries = data.get(group)
        if not isinstance(entries, list) or not entries:
            raise ValueError(f"{path}: '{group}' must be a non-empty list")
        names: set[str] = set()
        cleaned = []
        for index, entry in enumerate(entries):
            where = f"{path}: {group}[{index}]"
            if (
                not isinstance(entry, dict)
                or not {"name", "model"} <= set(entry)
                or set(entry) - {"name", "model", *OPTIONAL_FIELDS}
                or not all(isinstance(entry[k], str) and entry[k].strip() for k in ("name", "model"))
            ):
                raise ValueError(
                    f"{where} must have the string keys 'name' and 'model' and may have "
                    f"only these other keys: {', '.join(OPTIONAL_FIELDS)}"
                )
            if entry["name"] in names:
                raise ValueError(f"{path}: duplicate name {entry['name']!r} in '{group}'")
            names.add(entry["name"])
            clean: dict[str, Any] = {"name": entry["name"], "model": entry["model"]}
            for field in INT_FIELDS:
                value = entry.get(field)
                if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 1):
                    raise ValueError(f"{where}: '{field}' must be a positive whole number or empty")
                clean[field] = value
            effort = entry.get("reasoning_effort")
            if effort is not None and not (isinstance(effort, str) and effort.strip()):
                raise ValueError(f"{where}: 'reasoning_effort' must be a non-empty string or empty")
            clean["reasoning_effort"] = effort
            cleaned.append(clean)
        config[group] = cleaned
    return config
