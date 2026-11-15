"""Load and render the plain-text prompt templates in prompts/.

A placeholder is a lowercase identifier in braces, such as {proposition}.
Anything else in braces (for example the literal JSON format shown in the
probe templates) is left untouched.
"""

from __future__ import annotations

import hashlib
import random
import re
from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"

_PLACEHOLDER = re.compile(r"\{([a-z_][a-z0-9_]*)\}")


def load_template(name: str, prompts_dir: str | Path | None = None) -> str:
    """Return the text of prompts/<name>.txt without its trailing newline."""
    directory = Path(prompts_dir) if prompts_dir is not None else PROMPTS_DIR
    path = directory / f"{name}.txt"
    if not path.is_file():
        raise FileNotFoundError(f"prompt template not found: {path}")
    return path.read_text(encoding="utf-8").rstrip("\n")


def placeholders(template: str) -> set[str]:
    """Return the placeholder names used in a template."""
    return set(_PLACEHOLDER.findall(template))


def render(name: str, prompts_dir: str | Path | None = None, **values: str) -> str:
    """Render a template by name.

    Raises KeyError if the template needs a value that was not given, and
    ValueError if a value was given that the template does not use.
    """
    template = load_template(name, prompts_dir)
    needed = placeholders(template)
    missing = needed - values.keys()
    if missing:
        raise KeyError(f"template {name!r} is missing values for: {sorted(missing)}")
    unused = values.keys() - needed
    if unused:
        raise ValueError(f"template {name!r} does not use: {sorted(unused)}")
    # A single pass, so braces inside substituted values are never re-read.
    return _PLACEHOLDER.sub(lambda match: str(values[match.group(1)]), template)


def fact_order(facts, scenario_id: str) -> list:
    """Return the facts in a shuffled order that is fixed per scenario.

    The seed is derived from the scenario id alone, so the order is the same
    on every call, in every process, and for every condition and probe. It
    stops the authoring order (usually grouped by valence) from leaking.
    """
    digest = hashlib.sha256(f"dgap-fact-order:{scenario_id}".encode("utf-8")).digest()
    ordered = list(facts)
    random.Random(int.from_bytes(digest[:8], "big")).shuffle(ordered)
    return ordered


def format_facts(facts, scenario_id: str) -> str:
    """List fact texts one per line, in the scenario's fixed shuffled order.

    Only the text is shown: fact ids and valence labels never reach a model.
    """
    return "\n".join(f"- {fact.text}" for fact in fact_order(facts, scenario_id))
