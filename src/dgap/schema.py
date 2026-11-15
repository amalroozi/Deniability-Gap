"""Scenario schema and JSONL loader."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError, model_validator

NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class Fact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: NonEmptyStr
    text: NonEmptyStr
    valence: Literal["favorable", "adverse", "neutral"]


class Scenario(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: NonEmptyStr
    domain: NonEmptyStr
    advisor_role: NonEmptyStr
    user_query: NonEmptyStr
    facts: list[Fact] = Field(min_length=4)
    proposition: NonEmptyStr
    proposition_status: Literal["false", "unsupported"]
    # States the operator's commercial interest only (used in mild and strong).
    incentive_context: NonEmptyStr
    # States the impression the operator wants the user to have (strong only).
    incentive_goal: NonEmptyStr
    example: bool = False

    @model_validator(mode="after")
    def _check_facts(self) -> Scenario:
        if not any(fact.valence == "adverse" for fact in self.facts):
            raise ValueError("facts must include at least one adverse fact")
        ids = [fact.id for fact in self.facts]
        if len(ids) != len(set(ids)):
            raise ValueError("fact ids must be unique within a scenario")
        return self

    @model_validator(mode="after")
    def _check_proposition(self) -> Scenario:
        # The proposition is spliced into sentences such as
        # "are you saying that {proposition}?", so it must be a bare clause.
        if self.proposition[-1] in ".?!":
            raise ValueError("proposition must not end with punctuation")
        return self


def load_scenarios(path: str | Path) -> list[Scenario]:
    """Load and validate a JSONL file of scenarios.

    Blank lines are skipped. Any invalid line raises ValueError naming the
    file and the 1-based line number.
    """
    path = Path(path)
    scenarios: list[Scenario] = []
    seen: dict[str, int] = {}
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                scenario = Scenario.model_validate(json.loads(line))
            except (json.JSONDecodeError, ValidationError) as exc:
                raise ValueError(f"{path}: line {line_number}: {exc}") from exc
            if scenario.id in seen:
                raise ValueError(
                    f"{path}: line {line_number}: duplicate scenario id "
                    f"{scenario.id!r} (first used on line {seen[scenario.id]})"
                )
            seen[scenario.id] = line_number
            scenarios.append(scenario)
    return scenarios
