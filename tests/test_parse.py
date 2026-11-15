import pytest

from dgap.parse import (
    parse_judge_advice,
    parse_judge_confirm,
    parse_json_object,
    parse_probability,
    parse_yes_no,
)


@pytest.mark.parametrize(
    "text, expected",
    [
        ('{"probability": 70, "reason": "because"}', 70),
        ('{"probability": 0, "reason": ""}', 0),
        ('{"probability": 100, "reason": ""}', 100),
        ('```json\n{"probability": 35, "reason": "x"}\n```', 35),
        ('```\n{"probability": 35, "reason": "x"}\n```', 35),
        ('Sure, here is my answer:\n{"probability": 12, "reason": "x"}\nHope that helps.', 12),
        ('{"reason": "has {braces} inside", "probability": 55}', 55),
        ('{"probability": 70.0, "reason": "x"}', 70),
        ('{"probability": "70", "reason": "x"}', 70),
        ("Probability: 70", 70),
        # Truncated JSON: the labelled number is still unambiguous.
        ('{"probability": 70, "reason": "cut off mid', 70),
        ("42", 42),
        ("42%", 42),
    ],
)
def test_parse_probability_good(text, expected):
    assert parse_probability(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "I cannot answer that.",
        "MOCK RESPONSE abc123",
        '{"probability": 150, "reason": "x"}',
        '{"probability": -5, "reason": "x"}',
        '{"probability": 72.5, "reason": "x"}',
        '{"probability": true, "reason": "x"}',
        '{"probability": null, "reason": "x"}',
        '{"probability": "high", "reason": "x"}',
        '{"reason": "no number given"}',
        "Somewhere between 30 and 70 out of 100.",
        "probability: 30 at first, but probability: 70 on reflection",
        "101",
        None,
    ],
)
def test_parse_probability_garbage_is_none_not_a_guess(text):
    assert parse_probability(text) is None


@pytest.mark.parametrize(
    "text, expected",
    [
        ("YES. The fund is safe.", "YES"),
        ("NO, I am not saying that.", "NO"),
        ("yes", "YES"),
        ("No.", "NO"),
        ("  \n**No** - that is not what I meant.", "NO"),
        ('"Yes," in short.', "YES"),
    ],
)
def test_parse_yes_no_good(text, expected):
    assert parse_yes_no(text) == expected


@pytest.mark.parametrize(
    "text",
    ["", "Not exactly.", "Nobody can say.", "Yesterday I said so.", "Well, yes and no.", "MOCK RESPONSE", None],
)
def test_parse_yes_no_garbage(text):
    assert parse_yes_no(text) is None


def test_parse_json_object_good_fenced_and_embedded():
    assert parse_json_object('{"a": 1, "b": 2}', ["a"]) == {"a": 1, "b": 2}
    assert parse_json_object('```json\n{"a": 1}\n```', ["a"]) == {"a": 1}
    assert parse_json_object('text {"x": 0} more {"a": [1, {"k": 2}]} end', ["a"]) == {"a": [1, {"k": 2}]}


@pytest.mark.parametrize("text", ["", "no json here", "[1, 2, 3]", '{"a": 1', '{"b": 1}', "{a: 1}", None])
def test_parse_json_object_garbage(text):
    assert parse_json_object(text, ["a"]) is None


def test_parse_judge_advice():
    good = '{"states_p_explicitly": false, "contradicts_p_explicitly": true, "discloses_adverse_fact": true}'
    assert parse_judge_advice(f"```json\n{good}\n```") == {
        "states_p_explicitly": False,
        "contradicts_p_explicitly": True,
        "discloses_adverse_fact": True,
    }
    assert parse_judge_advice('{"states_p_explicitly": false, "contradicts_p_explicitly": true}') is None
    assert parse_judge_advice(good.replace("false", '"no"')) is None
    assert parse_judge_advice("garbage") is None


def test_parse_judge_confirm():
    assert parse_judge_confirm('{"label": "DENY_REPAIR"}') == "DENY_REPAIR"
    assert parse_judge_confirm('Here you go: {"label": "EVADE"}') == "EVADE"
    assert parse_judge_confirm('{"label": "MAYBE"}') is None
    assert parse_judge_confirm('{"label": "deny"}') is None
    assert parse_judge_confirm("DENY") is None
