"""Solution tags from info.json: grammar, uniqueness, and verdict matching."""

import pytest
from pydantic import ValidationError

from olympdev.problem.types import (
    ProblemConfiguration,
    ProblemSolution,
    match_solution_tag,
    parse_solution_tag,
)
from olympdev.worker.verdict import Verdict


def _sol(tag: str) -> ProblemSolution:
    return ProblemSolution.model_validate({"file": "sol.cpp", "tag": tag})


@pytest.mark.parametrize(
    "tag,permissible,required,any_predictable,ignored",
    [
        ("main", (Verdict.OK,), (), False, False),
        ("OK", (Verdict.OK,), (), False, False),
        ("WA", (Verdict.OK, Verdict.WA), (Verdict.WA,), False, False),
        ("TL", (Verdict.OK, Verdict.TL), (Verdict.TL,), False, False),
        ("ML", (Verdict.OK, Verdict.ML), (Verdict.ML,), False, False),
        ("RE", (Verdict.OK, Verdict.RE), (Verdict.RE,), False, False),
        ("PT", (Verdict.OK, Verdict.PT), (Verdict.PT,), False, False),
        ("TL?", (Verdict.OK, Verdict.TL), (), False, False),
        (
            "TL+ML?",
            (Verdict.OK, Verdict.TL, Verdict.ML),
            (Verdict.TL,),
            False,
            False,
        ),
        (
            "TL+WA",
            (Verdict.OK, Verdict.WA, Verdict.TL),
            (Verdict.TL, Verdict.WA),
            False,
            False,
        ),
        ("CE?", (Verdict.OK, Verdict.CE), (), False, False),
        (
            "incor",
            (Verdict.OK, Verdict.WA, Verdict.TL, Verdict.ML, Verdict.RE, Verdict.PT),
            (),
            True,
            False,
        ),
        ("ignore", (), (), False, True),
    ],
)
def test_parse_solution_tag(
    tag, permissible, required, any_predictable, ignored
):
    expectation = parse_solution_tag(tag)
    assert expectation.permissible == permissible
    assert expectation.required == required
    assert expectation.require_any_predictable is any_predictable
    assert expectation.ignored is ignored
    solution = _sol(tag)
    assert solution.get_permissible_verdicts() == list(permissible)
    assert solution.get_expected_verdicts() == list(required)
    assert solution.is_ignored() is ignored


@pytest.mark.parametrize(
    "tag",
    ["alt", "wa", "WA+", "+WA", "WA++TL", "CE", "OK+WA", "main+WA", "WA+WA", ""],
)
def test_invalid_tags_are_rejected(tag):
    with pytest.raises(ValueError, match="invalid solution tag|duplicate token"):
        parse_solution_tag(tag)
    with pytest.raises(ValidationError, match="invalid solution tag|duplicate token"):
        _sol(tag)


def test_duplicate_token_with_optional_is_rejected():
    with pytest.raises(ValueError, match="duplicate token"):
        parse_solution_tag("WA+WA?")


@pytest.mark.parametrize(
    "tag,observed,reason",
    [
        ("WA", [Verdict.OK, Verdict.WA], None),
        ("WA", [Verdict.OK, Verdict.OK], "missing required verdict WA"),
        ("WA", [Verdict.OK, Verdict.RE], "unexpected verdict RE"),
        ("TL?", [Verdict.OK, Verdict.OK], None),
        ("TL?", [Verdict.OK, Verdict.TL], None),
        ("TL?", [Verdict.OK, Verdict.WA], "unexpected verdict WA"),
        ("TL+ML?", [Verdict.OK, Verdict.TL], None),
        ("TL+ML?", [Verdict.TL, Verdict.ML], None),
        ("TL+ML?", [Verdict.OK, Verdict.ML], "missing required verdict TL"),
        ("TL+WA", [Verdict.TL, Verdict.WA], None),
        ("TL+WA", [Verdict.TL, Verdict.OK], "missing required verdict WA"),
        ("incor", [Verdict.OK, Verdict.WA], None),
        ("incor", [Verdict.OK, Verdict.OK], "expected a predictable error"),
        ("incor", [Verdict.OK, Verdict.CE], "unexpected verdict CE"),
        ("main", [Verdict.OK, Verdict.OK], None),
        ("main", [Verdict.OK, Verdict.WA], "unexpected verdict WA"),
        ("OK", [Verdict.CE], "unexpected verdict CE"),
        ("ignore", [Verdict.WA], None),
        ("CE?", [Verdict.OK], None),
        ("CE?", [Verdict.CE], None),
    ],
)
def test_match_solution_tag(tag, observed, reason):
    actual = match_solution_tag(parse_solution_tag(tag), observed)
    if reason is None:
        assert actual is None
    else:
        assert actual is not None and reason in actual


def test_compound_and_optional_tags_are_unique_like_any_other():
    with pytest.raises(ValidationError, match="duplicate solution tag 'TL\\+ML\\?'"):
        ProblemConfiguration.model_validate(
            {
                "time_limit_ms": 1000,
                "memory_limit_mb": 64,
                "solutions": [
                    {"file": "a.cpp", "tag": "main"},
                    {"file": "b.cpp", "tag": "TL+ML?"},
                    {"file": "c.cpp", "tag": "TL+ML?"},
                ],
            }
        )


def test_new_tags_load_alongside_main():
    config = ProblemConfiguration.model_validate(
        {
            "time_limit_ms": 1000,
            "memory_limit_mb": 64,
            "solutions": [
                {"file": "a.cpp", "tag": "main"},
                {"file": "b.cpp", "tag": "OK"},
                {"file": "c.cpp", "tag": "incor"},
                {"file": "d.cpp", "tag": "ignore"},
                {"file": "e.cpp", "tag": "TL+ML?"},
            ],
        }
    )
    assert [s.tag for s in config.solutions] == [
        "main",
        "OK",
        "incor",
        "ignore",
        "TL+ML?",
    ]
