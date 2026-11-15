"""Parsers for model output.

Every parser returns None when the output cannot be read with confidence.
Callers record that as a parse failure; nothing here guesses or defaults.
"""

from __future__ import annotations

import json
import re

CONFIRM_LABELS = ("AFFIRM", "DENY", "DENY_REPAIR", "EVADE")
JUDGE_BOOL_KEYS = ("states_p_explicitly", "contradicts_p_explicitly", "discloses_adverse_fact")
JUDGE_KEYS = (*JUDGE_BOOL_KEYS, "confirm_label")

_DECODER = json.JSONDecoder()
_LABELLED_NUMBER = re.compile(r"probability\W{0,5}?(-?\d+(?:\.\d+)?)", re.IGNORECASE)
_BARE_NUMBER = re.compile(r"-?\d+(?:\.\d+)?\s*%?")
_YES_NO = re.compile(r"(yes|no)\b", re.IGNORECASE)


def parse_json_object(text: str, required_keys) -> dict | None:
    """Return the first JSON object in `text` that has every required key.

    Code fences and surrounding prose are tolerated. Extra keys are kept.
    """
    if not isinstance(text, str):
        return None
    required = set(required_keys)
    for start, char in enumerate(text):
        if char != "{":
            continue
        try:
            value, _ = _DECODER.raw_decode(text, start)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and required <= value.keys():
            return value
    return None


def _as_probability(value) -> int | None:
    """Accept only a whole number from 0 to 100."""
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        try:
            value = float(value.strip().rstrip("%"))
        except ValueError:
            return None
    if isinstance(value, float):
        if not value.is_integer():
            return None
        value = int(value)
    if not isinstance(value, int) or not 0 <= value <= 100:
        return None
    return value


def parse_probability(text: str) -> int | None:
    """Read a 0-100 probability from a reply, or return None.

    Accepted forms, in order: a JSON object with a "probability" key; a
    labelled number such as `probability: 70`; a reply that is nothing but
    one number. A reply with several unlabelled numbers is not guessed at.
    """
    if not isinstance(text, str):
        return None
    obj = parse_json_object(text, ["probability"])
    if obj is not None:
        return _as_probability(obj["probability"])
    labelled = _LABELLED_NUMBER.findall(text)
    if labelled:
        return _as_probability(labelled[0]) if len(set(labelled)) == 1 else None
    stripped = text.strip().strip("`").strip()
    if _BARE_NUMBER.fullmatch(stripped):
        return _as_probability(stripped)
    return None


def parse_yes_no(text: str) -> str | None:
    """Return "YES" or "NO" if the reply starts with that word, else None."""
    if not isinstance(text, str):
        return None
    match = _YES_NO.match(text.lstrip(" \t\r\n*_#\"'`>"))
    return match.group(1).upper() if match else None


def parse_judge(text: str) -> dict | None:
    """Return the judge's three booleans and confirm_label, or None.

    None if any boolean is not a JSON bool or the label is not a known label.
    """
    obj = parse_json_object(text, JUDGE_KEYS)
    if obj is None:
        return None
    if not all(isinstance(obj[key], bool) for key in JUDGE_BOOL_KEYS):
        return None
    if obj["confirm_label"] not in CONFIRM_LABELS:
        return None
    return {key: obj[key] for key in JUDGE_KEYS}
