"""Group-points scoring, end to end against ``example-contest/points``.

Loads a solution's real verdicts through the ordinary
``ProblemTestingPipeline`` (compiles, runs in isolate, checks) and feeds
them into ``problem/scoring.py``, exactly the way ``verify`` would once it
grows a score report. See TODO.md: Subtasks.
"""

import shutil
from pathlib import Path

import pytest

from olympdev.frontend.view import VerificationReportView
from olympdev.isolate import IsolateFactory
from olympdev.problem.build import build_problem_testing_input, build_problem_verification_input
from olympdev.problem.package import ProblemPackage
from olympdev.problem.scoring import check_solution_group_tags, score_solution
from olympdev.storage.filestorage import FileStorage
from olympdev.worker.pipeline import PipelineContext, run_pipeline
from olympdev.worker.pipelines.problem_testing import ProblemTestingPipeline
from olympdev.worker.pipelines.problem_verification import ProblemVerificationPipeline
from olympdev.worker.verdict import Verdict

pytestmark = pytest.mark.slow


@pytest.fixture(scope="session")
def points_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "example-contest" / "points"


def _run_solution(
    storage: FileStorage,
    isolate_factory: IsolateFactory,
    points_dir: Path,
    solution_file: str,
) -> tuple[ProblemPackage, dict[str, Verdict]]:
    if shutil.which("g++") is None:
        pytest.skip("g++ is not available")
    package = ProblemPackage.load(points_dir)
    digest = build_problem_testing_input(storage, package, solution=solution_file)
    pipeline = ProblemTestingPipeline(f"points-{Path(solution_file).stem}")
    run_pipeline(
        pipeline,
        PipelineContext(input_digest=digest, storage=storage, isolate_factory=isolate_factory),
    )
    result = pipeline.get_result(storage)
    assert result is not None
    return package, {test.name: test.verdict for test in result.tests}


def test_correct_solution_scores_full_marks(
    storage: FileStorage, isolate_factory: IsolateFactory, points_dir: Path
):
    package, verdicts = _run_solution(
        storage, isolate_factory, points_dir, "solutions/sol-ac.cpp"
    )
    assert verdicts == {
        "tests/g1-01.txt": Verdict.OK,
        "tests/g2-01.txt": Verdict.OK,
    }

    report = score_solution(package.configuration, verdicts)
    assert report.max_points == 100
    assert report.total_points == 100
    assert all(group.passed for group in report.groups)
    assert report.group(1).points_earned == 40
    assert report.group(2).points_earned == 60


def test_overflowing_solution_loses_only_the_large_group(
    storage: FileStorage, isolate_factory: IsolateFactory, points_dir: Path
):
    package, verdicts = _run_solution(
        storage, isolate_factory, points_dir, "solutions/sol-overflow.cpp"
    )
    assert verdicts["tests/g1-01.txt"] is Verdict.OK
    assert verdicts["tests/g2-01.txt"] is Verdict.WA

    report = score_solution(package.configuration, verdicts)
    assert report.total_points == 40
    assert report.group(1).passed and report.group(1).points_earned == 40
    assert not report.group(2).passed and report.group(2).points_earned == 0
    assert report.group(2).failing_tests == ["tests/g2-01.txt"]

    solution = package.solution("sol-overflow")
    mismatches = check_solution_group_tags(package.configuration, solution, verdicts)
    assert mismatches == {1: None, 2: None}


def test_group_tags_catch_a_solution_that_fails_the_wrong_group(
    storage: FileStorage, isolate_factory: IsolateFactory, points_dir: Path
):
    """The point of group_tags: a mismatch names which group broke the claim."""
    package, verdicts = _run_solution(
        storage, isolate_factory, points_dir, "solutions/sol-overflow.cpp"
    )
    solution = package.solution("sol-overflow")
    # sol-overflow actually is WA only on group 2 - assert what a wrong claim
    # ("WA on group 1 instead") would have caught.
    wrong_claim = solution.model_copy(update={"group_tags": {"1": "WA", "2": "OK"}})
    mismatches = check_solution_group_tags(package.configuration, wrong_claim, verdicts)
    assert mismatches[1] is not None and "missing required verdict WA" in mismatches[1]
    assert mismatches[2] is not None and "unexpected verdict WA" in mismatches[2]


def _verify_report(
    storage: FileStorage, isolate_factory: IsolateFactory, points_dir: Path
) -> VerificationReportView:
    if shutil.which("g++") is None:
        pytest.skip("g++ is not available")
    package = ProblemPackage.load(points_dir)
    digest = build_problem_verification_input(storage, package)
    pipeline = ProblemVerificationPipeline("points-verify")
    run_pipeline(
        pipeline,
        PipelineContext(input_digest=digest, storage=storage, isolate_factory=isolate_factory),
    )
    return VerificationReportView.from_pipeline(
        pipeline,
        storage,
        problem_name=package.name,
        configuration=package.configuration,
        solutions=package.verifiable_solutions(),
    )


def _column(view: VerificationReportView, tag: str):
    return next(column for column in view.columns if column.tag == tag)


def test_verify_report_scores_the_main_solution_full_marks(
    storage: FileStorage, isolate_factory: IsolateFactory, points_dir: Path
):
    view = _verify_report(storage, isolate_factory, points_dir)
    assert view.matched is True
    main = _column(view, "main")
    assert main.matched is True
    assert main.total_points == 100 and main.max_points == 100
    assert {group.group_id: group.points_earned for group in main.groups} == {1: 40, 2: 60}
    assert all(not group.tag_checked for group in main.groups)


def test_verify_report_scores_the_declared_overflow_solution(
    storage: FileStorage, isolate_factory: IsolateFactory, points_dir: Path
):
    view = _verify_report(storage, isolate_factory, points_dir)
    overflow = _column(view, "WA")
    assert overflow.total_points == 40 and overflow.max_points == 100
    assert overflow.matched is True, "declared group_tags match what actually happened"
    group2 = next(group for group in overflow.groups if group.group_id == 2)
    assert group2.points_earned == 0 and group2.failing_tests == ["tests/g2-01.txt"]
    assert group2.tag_checked is True and group2.tag_reason is None


def test_verify_report_flags_a_wrong_group_tags_claim(
    storage: FileStorage, isolate_factory: IsolateFactory, points_dir: Path
):
    """A solution whose group_tags claim doesn't match reality is caught by
    name: the mismatch reason on the column names the group and the test."""
    if shutil.which("g++") is None:
        pytest.skip("g++ is not available")
    package = ProblemPackage.load(points_dir)
    overflow = package.solution("sol-overflow")
    wrong_claim = overflow.model_copy(update={"group_tags": {"1": "WA", "2": "OK"}})
    testing_digest = build_problem_testing_input(storage, package, solution=wrong_claim)
    from olympdev.worker.pipelines.problem_verification import SolutionVerificationSpec

    digest = ProblemVerificationPipeline.make_input_digest(
        storage,
        solutions=[
            SolutionVerificationSpec(
                name=wrong_claim.name,
                tag=wrong_claim.tag,
                testing_digest=testing_digest,
            )
        ],
    )
    pipeline = ProblemVerificationPipeline("points-verify-wrong-claim")
    run_pipeline(
        pipeline,
        PipelineContext(input_digest=digest, storage=storage, isolate_factory=isolate_factory),
    )
    view = VerificationReportView.from_pipeline(
        pipeline,
        storage,
        problem_name=package.name,
        configuration=package.configuration,
        solutions=[wrong_claim],
    )
    column = view.columns[0]
    assert column.matched is False
    assert column.reason is not None
    assert "group 1" in column.reason and "missing required verdict WA" in column.reason
    assert "group 2" in column.reason
    assert "unexpected verdict WA on tests/g2-01.txt" in column.reason
    # The reason names the offending test once; it used to be spliced in
    # twice, from the group's failing_tests and from the mismatch itself.
    assert column.reason.count("tests/g2-01.txt") == 1
    group1 = next(group for group in column.groups if group.group_id == 1)
    group2 = next(group for group in column.groups if group.group_id == 2)
    assert group1.tag_checked and "missing required verdict WA" in group1.tag_reason
    assert group2.tag_checked and "unexpected verdict WA" in group2.tag_reason

