"""Per-group scoring for problems with ``mode: "group-points"``.

See TODO.md: Subtasks. Pure computation over verdicts a pipeline already
produced - no isolate, no storage, no sandbox. Lives next to
``problem/types.py`` because it only needs a :class:`ProblemConfiguration`
and a verdict per test name, the same names
``ProblemTestingResult.tests[].name`` already uses.
"""

from __future__ import annotations

from olympdev.problem.types import (
    ProblemConfiguration,
    ProblemGroup,
    ProblemScoringMode,
    ProblemSolution,
    match_solution_tag,
)
from olympdev.schema import FrozenModel
from olympdev.worker.verdict import Verdict


class GroupScore(FrozenModel):
    group: ProblemGroup
    passed: bool
    points_earned: int
    failing_tests: list[str] = []


class ScoreReport(FrozenModel):
    groups: list[GroupScore]

    @property
    def total_points(self) -> int:
        return sum(group.points_earned for group in self.groups)

    @property
    def max_points(self) -> int:
        return sum(group.group.points for group in self.groups)

    def group(self, group_id: int) -> GroupScore:
        for group in self.groups:
            if group.group.id == group_id:
                return group
        raise KeyError(group_id)


def _tests_by_group(configuration: ProblemConfiguration) -> dict[int, list[str]]:
    tests_by_group: dict[int, list[str]] = {
        group.id: [] for group in configuration.groups
    }
    for test in configuration.tests:
        assert test.group_id is not None  # enforced by ProblemConfiguration
        tests_by_group[test.group_id].append(test.display_name())
    return tests_by_group


def score_solution(
    configuration: ProblemConfiguration, verdicts: dict[str, Verdict]
) -> ScoreReport:
    """Score one solution's run against ``configuration.groups``.

    ``verdicts`` maps each test's display name to the verdict it got. A
    group's points are earned only if every one of its tests came back OK
    (TODO.md: Subtasks - "начислять баллы только за полностью решённую
    подзадачу"); ``depends_on`` does not cascade a zero, by design (see
    :class:`ProblemGroup`).
    """
    if configuration.mode is not ProblemScoringMode.GROUP_POINTS:
        raise ValueError(
            f"scoring requires mode={ProblemScoringMode.GROUP_POINTS.value!r}, "
            f"got {configuration.mode!r}"
        )
    tests_by_group = _tests_by_group(configuration)
    groups: list[GroupScore] = []
    for group in configuration.groups:
        names = tests_by_group[group.id]
        failing = [name for name in names if verdicts.get(name) is not Verdict.OK]
        passed = not failing
        groups.append(
            GroupScore(
                group=group,
                passed=passed,
                points_earned=group.points if passed else 0,
                failing_tests=failing,
            )
        )
    return ScoreReport(groups=groups)


def check_solution_group_tags(
    configuration: ProblemConfiguration,
    solution: ProblemSolution,
    verdicts: dict[str, Verdict],
) -> dict[int, str | None]:
    """Match ``solution.group_tags`` against observed per-group verdicts.

    Returns, for every group that has an entry in ``group_tags`` (or every
    group in the problem, if ``group_tags`` is set at all - see
    :meth:`ProblemSolution.group_tag_expectation`), the mismatch reason from
    ``match_solution_tag``, or ``None`` when it matched.
    """
    if solution.group_tags is None:
        raise ValueError(f"{solution.file}: no group_tags declared")
    tests_by_group = _tests_by_group(configuration)
    results: dict[int, str | None] = {}
    for group in configuration.groups:
        expectation = solution.group_tag_expectation(group.id)
        names = [name for name in tests_by_group[group.id] if name in verdicts]
        observed = [verdicts[name] for name in names]
        results[group.id] = match_solution_tag(expectation, observed, names)
    return results
