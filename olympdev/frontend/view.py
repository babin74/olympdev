"""View models: what to show, with no idea how to show it.

These carry every decision that is about the domain rather than the medium -
which number counts as "time used", how a column's verdict is summarised,
where a result is read from - so the CLI, the HTML templates and a future web
API all render the same facts instead of each recomputing them.

Nothing here imports jinja2: a worker installed without the notebook extra
must still be able to build and print a report.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Self

from olympdev.isolate import IsolateMeta
from olympdev.problem.scoring import check_solution_group_tags, score_solution
from olympdev.problem.types import ProblemConfiguration, ProblemScoringMode, ProblemSolution
from olympdev.schema import FrozenModel
from olympdev.storage import EMPTY_BLOB_DIGEST, AbstractStorage
from olympdev.worker.job import AbstractJob, JobStatus
from olympdev.worker.pipeline import AbstractPipeline, PipelineStatus
from olympdev.worker.pipelines.problem_test import ProblemTestResult
from olympdev.worker.pipelines.problem_testing import ProblemTestingResult
from olympdev.worker.pipelines.problem_verification import ProblemVerificationResult
from olympdev.worker.pipelines.statement import StatementPipeline
from olympdev.worker.pipelines.stress import StressResult
from olympdev.worker.verdict import Verdict

if TYPE_CHECKING:
    from olympdev.worker.jobs.sandbox import SandboxJobResult


def _duration_number(milliseconds: float | None) -> str:
    if milliseconds is None:
        return "—"
    if milliseconds < 10:
        return f"{milliseconds:.1f}"
    return f"{milliseconds:.0f}"


def format_duration(milliseconds: float | None) -> str:
    if milliseconds is None:
        return "—"
    return f"{_duration_number(milliseconds)} ms"


def format_duration_pair(
    cpu_ms: float | None, wall_ms: float | None
) -> str:
    """CPU and wall time as ``<time>/<wall>``, sharing one unit."""
    if cpu_ms is None and wall_ms is None:
        return "—"
    return f"{_duration_number(cpu_ms)}/{_duration_number(wall_ms)} ms"


def format_memory(kilobytes: int | None) -> str:
    if kilobytes is None:
        return "—"
    if kilobytes >= 1024:
        return f"{kilobytes / 1024:.1f} MB"
    return f"{kilobytes} KB"


class UsageView(FrozenModel):
    """Time and memory a run actually used.

    Both clocks are kept: isolate's ``time`` is CPU, ``time_wall`` is elapsed.
    Either alone misleads - a solution that sleeps burns almost no CPU and
    would look instant, while a busy loop on a loaded host looks slower than
    it is. They are shown together as ``<time>/<wall>``.
    """

    cpu_ms: float | None = None
    wall_ms: float | None = None
    memory_kb: int | None = None

    @classmethod
    def from_meta(cls, meta: IsolateMeta) -> Self:
        return cls(
            cpu_ms=meta.time * 1000.0,
            wall_ms=meta.time_wall * 1000.0,
            memory_kb=meta.cg_memory_kb or meta.max_rss_kb or None,
        )

    @property
    def time_text(self) -> str:
        """CPU and wall time as ``<time>/<wall>``."""
        return format_duration_pair(self.cpu_ms, self.wall_ms)

    @property
    def cpu_text(self) -> str:
        return format_duration(self.cpu_ms)

    @property
    def wall_text(self) -> str:
        return format_duration(self.wall_ms)

    @property
    def memory_text(self) -> str:
        return format_memory(self.memory_kb)


EMPTY_USAGE = UsageView()


class JobView(FrozenModel):
    job_id: str
    job_name: str
    job_description: str = ""
    job_status: JobStatus = JobStatus.PENDING
    job_result: str = ""
    job_digests: list[tuple[str, str]] = []
    job_labels: list[tuple[str, str]] = []

    @classmethod
    def from_job(cls, job: AbstractJob) -> Self:
        return cls(
            job_id=job.job_id,
            job_name=type(job).__name__,
            job_description=job.get_description(),
            job_status=job.get_status(),
            job_result=job.get_output_digest() or "",
            job_labels=list(job.labels.items()),
        )

    def _repr_html_(self) -> str:
        from olympdev.frontend.html import render_job

        return render_job(self)


class TestRowView(FrozenModel):
    name: str
    verdict: Verdict
    usage: UsageView = EMPTY_USAGE
    run_digest: str | None = None
    check_digest: str | None = None
    #: What the solution wrote on this test, straight from the sandbox. The
    #: answer to a test is ``stdout_digest`` of a run of the main solution;
    #: ``stderr_digest`` is where a crash explains itself. Both are None when
    #: the stream was empty - see :func:`_run_details`.
    stdout_digest: str | None = None
    stderr_digest: str | None = None
    #: What the solution was fed, file-backed or generated.
    input_digest: str | None = None
    #: Which group this test scores into, when the problem uses group-points.
    #: None for an ungrouped problem, or when no configuration was given.
    group_id: int | None = None

    @classmethod
    def from_result(
        cls,
        storage: AbstractStorage,
        result: ProblemTestResult,
        *,
        group_id: int | None = None,
    ) -> Self:
        usage, stdout_digest, stderr_digest = _run_details(storage, result.run_digest)
        return cls(
            name=result.name,
            verdict=result.verdict,
            usage=usage,
            run_digest=result.run_digest,
            check_digest=result.check_digest,
            stdout_digest=stdout_digest,
            stderr_digest=stderr_digest,
            input_digest=_non_empty(result.input_digest),
            group_id=group_id,
        )


def _non_empty(digest: str | None) -> str | None:
    """Drop a digest that points at zero bytes - it is nothing to look at."""
    return None if digest is None or digest == EMPTY_BLOB_DIGEST else digest


def _run_details(
    storage: AbstractStorage, run_digest: str | None
) -> tuple[UsageView, str | None, str | None]:
    """Usage and the run's output digests, from the one blob that holds them.

    Timing and the stdout/stderr digests all live in the same
    ``SandboxJobResult``, so a report that shows both costs no more storage
    round trips than one that shows only the clock.
    """
    if run_digest is None:
        return EMPTY_USAGE, None, None
    from olympdev.worker.jobs.sandbox import SandboxJobResult

    result: SandboxJobResult | None = SandboxJobResult.from_storage(
        storage, run_digest
    )
    if result is None:
        return EMPTY_USAGE, None, None
    return (
        UsageView.from_meta(result.meta),
        _non_empty(result.stdout_digest),
        _non_empty(result.stderr_digest),
    )


#: How many failing tests a report explains before it starts repeating itself.
#: A solution that times out does so on many tests; the first few carry the
#: diagnosis, the rest are noise.
_MAX_DIAGNOSTICS = 3


def _diagnostics(tests: list["TestRowView"], limit: int) -> list["TestRowView"]:
    """Failing tests that left something to read behind."""
    return [
        test
        for test in tests
        if test.verdict is not Verdict.OK and test.stderr_digest is not None
    ][:limit]


def _cpu_seconds(tests: list["TestRowView"]) -> float | None:
    """CPU actually burned across a run's tests."""
    values = [test.usage.cpu_ms for test in tests if test.usage.cpu_ms is not None]
    return sum(values) / 1000.0 if values else None


def _peak_usage(tests: list["TestRowView"]) -> tuple[float | None, float | None]:
    """The worst single test: peak CPU and peak wall time, independently.

    Each clock is maxed on its own - the test that used the most CPU need not
    be the one that took the longest wall time - so together they answer "how
    close did this solution get to TL/ML on its hardest test", which a sum
    across every test does not.
    """
    cpu = [test.usage.cpu_ms for test in tests if test.usage.cpu_ms is not None]
    wall = [test.usage.wall_ms for test in tests if test.usage.wall_ms is not None]
    return (max(cpu) if cpu else None, max(wall) if wall else None)


class PipelineRunView(FrozenModel):
    """One pipeline run: its outcome, its jobs and its per-test results."""

    pipeline_id: str
    status: PipelineStatus
    verdict: Verdict | None = None
    failure_reason: str | None = None
    output_digest: str | None = None
    problem_name: str | None = None
    wall_seconds: float | None = None
    jobs: list[JobView] = []
    tests: list[TestRowView] = []

    @classmethod
    def from_pipeline(
        cls,
        pipeline: AbstractPipeline,
        storage: AbstractStorage,
        *,
        problem_name: str | None = None,
        wall_seconds: float | None = None,
        jobs: list[JobView] | None = None,
    ) -> Self:
        result = _testing_result(pipeline, storage)
        return cls(
            pipeline_id=pipeline.pipeline_id,
            status=pipeline.get_status(),
            verdict=None if result is None else result.verdict,
            failure_reason=(
                None
                if pipeline.get_failure_reason() is None
                else str(pipeline.get_failure_reason())
            ),
            output_digest=pipeline.get_output_digest(),
            problem_name=problem_name,
            wall_seconds=wall_seconds,
            jobs=jobs or [],
            tests=(
                []
                if result is None
                else [TestRowView.from_result(storage, test) for test in result.tests]
            ),
        )

    @property
    def verdict_text(self) -> str:
        return "—" if self.verdict is None else self.verdict.value

    @property
    def diagnostics(self) -> list[TestRowView]:
        """Failing tests worth pointing a reader at, worst-first by position."""
        return _diagnostics(self.tests, _MAX_DIAGNOSTICS)

    @property
    def cpu_seconds(self) -> float | None:
        return _cpu_seconds(self.tests)

    @property
    def time_text(self) -> str:
        """CPU burned by the solution over wall time of the whole run."""
        return format_duration_pair(
            None if self.cpu_seconds is None else self.cpu_seconds * 1000.0,
            None if self.wall_seconds is None else self.wall_seconds * 1000.0,
        )

    def _repr_html_(self) -> str:
        from olympdev.frontend.html import render_pipeline_run

        return render_pipeline_run(self)


def _testing_result(
    pipeline: AbstractPipeline, storage: AbstractStorage
) -> ProblemTestingResult | None:
    digest = pipeline.get_output_digest()
    if digest is None:
        return None
    return ProblemTestingResult.from_storage(storage, digest)


class GroupScoreView(FrozenModel):
    """One group's score for one solution, when the problem uses group-points."""

    group_id: int
    name: str
    max_points: int
    points_earned: int
    passed: bool
    failing_tests: list[str] = []
    #: Whether this solution declared ``group_tags`` (so this group's verdicts
    #: were checked against a claim); False means score-only, never checked.
    tag_checked: bool = False
    tag_reason: str | None = None

    @property
    def score_text(self) -> str:
        return f"{self.points_earned}/{self.max_points}"


class SolutionColumnView(FrozenModel):
    """One solution's results, as a column of the comparison matrix."""

    name: str
    tag: str | None = None
    verdict: Verdict | None = None
    matched: bool | None = None
    reason: str | None = None
    tests: list[TestRowView] = []
    #: Populated only when the problem uses group-points and a configuration
    #: was supplied to ``VerificationReportView.from_pipeline``.
    groups: list[GroupScoreView] = []
    total_points: int | None = None
    max_points: int | None = None

    @property
    def verdict_text(self) -> str:
        return "—" if self.verdict is None else self.verdict.value

    @property
    def match_text(self) -> str:
        if self.matched is None:
            return "—"
        return "match" if self.matched else "mismatch"

    @property
    def score_text(self) -> str:
        if self.total_points is None:
            return "—"
        return f"{self.total_points}/{self.max_points}"

    @property
    def diagnostics(self) -> list[TestRowView]:
        """Failing tests to explain - only when the solution did not match.

        A WA on a solution tagged ``WA`` is the point of that solution, not a
        symptom, so a matching column has nothing to diagnose.
        """
        if self.matched is not False:
            return []
        return _diagnostics(self.tests, 1)

    @property
    def peak_cpu_ms(self) -> float | None:
        return _peak_usage(self.tests)[0]

    @property
    def peak_wall_ms(self) -> float | None:
        return _peak_usage(self.tests)[1]

    @property
    def time_text(self) -> str:
        """Peak CPU and peak wall time across this solution's tests."""
        return format_duration_pair(*_peak_usage(self.tests))

    def test(self, index: int) -> TestRowView | None:
        return self.tests[index] if index < len(self.tests) else None


class VerificationReportView(FrozenModel):
    """Verification of prepared solutions against their tags."""

    matched: bool
    status: PipelineStatus
    problem_name: str | None = None
    failure_reason: str | None = None
    wall_seconds: float | None = None
    jobs: list[JobView] = []
    test_names: list[str] = []
    columns: list[SolutionColumnView] = []

    @classmethod
    def from_pipeline(
        cls,
        pipeline: AbstractPipeline,
        storage: AbstractStorage,
        *,
        problem_name: str | None = None,
        wall_seconds: float | None = None,
        jobs: list[JobView] | None = None,
        configuration: ProblemConfiguration | None = None,
        solutions: list[ProblemSolution] | None = None,
    ) -> Self:
        """``configuration``/``solutions`` are optional and additive: when
        ``configuration`` is omitted (every caller before group-points scoring
        existed), the report is unchanged. When given and the problem's
        ``mode`` is ``"group-points"``, each column also gets a per-group
        score (see :mod:`olympdev.problem.scoring`) computed here, on the
        submitter side - the worker-side pipeline never sees a
        ``ProblemConfiguration``.
        """
        result = _verification_result(pipeline, storage)
        solution_by_name = {item.name: item for item in (solutions or [])}
        group_id_by_test_name = _group_id_by_test_name(configuration)
        columns: list[SolutionColumnView] = []
        if result is not None:
            for spec in result.solutions:
                testing = ProblemTestingResult.from_storage(
                    storage, spec.testing_digest
                )
                tests = (
                    []
                    if testing is None
                    else [
                        TestRowView.from_result(
                            storage, test, group_id=group_id_by_test_name.get(test.name)
                        )
                        for test in testing.tests
                    ]
                )
                matched = spec.matched
                reason = spec.reason
                groups: list[GroupScoreView] = []
                total_points: int | None = None
                max_points: int | None = None
                if (
                    testing is not None
                    and configuration is not None
                    and configuration.mode is ProblemScoringMode.GROUP_POINTS
                ):
                    verdicts = {test.name: test.verdict for test in testing.tests}
                    score = score_solution(configuration, verdicts)
                    total_points = score.total_points
                    max_points = score.max_points
                    matched_solution = solution_by_name.get(spec.name)
                    tag_checked = (
                        matched_solution is not None
                        and matched_solution.group_tags is not None
                    )
                    tag_reasons = (
                        check_solution_group_tags(configuration, matched_solution, verdicts)
                        if tag_checked
                        else {}
                    )
                    for group_score in score.groups:
                        tag_reason = tag_reasons.get(group_score.group.id)
                        if tag_reason is not None:
                            # tag_reason already names the offending test.
                            named_reason = f"group {group_score.group.id}: {tag_reason}"
                            reason = _merge_reason(reason, named_reason)
                            matched = False
                        groups.append(
                            GroupScoreView(
                                group_id=group_score.group.id,
                                name=group_score.group.name,
                                max_points=group_score.group.points,
                                points_earned=group_score.points_earned,
                                passed=group_score.passed,
                                failing_tests=group_score.failing_tests,
                                tag_checked=tag_checked,
                                tag_reason=tag_reasons.get(group_score.group.id),
                            )
                        )
                    if spec.tag in ("main", "OK") and total_points < max_points:
                        matched = False
                        reason = _merge_reason(
                            reason,
                            f"scored {total_points}/{max_points}, "
                            f"expected {max_points}/{max_points}",
                        )
                columns.append(
                    SolutionColumnView(
                        name=spec.name,
                        tag=spec.tag,
                        verdict=None if testing is None else testing.verdict,
                        matched=matched,
                        reason=reason,
                        tests=tests,
                        groups=groups,
                        total_points=total_points,
                        max_points=max_points,
                    )
                )
        test_names: list[str] = []
        for column in columns:
            if len(column.tests) > len(test_names):
                test_names = [test.name for test in column.tests]
        return cls(
            matched=False if result is None else result.matched,
            status=pipeline.get_status(),
            problem_name=problem_name,
            failure_reason=(
                None
                if pipeline.get_failure_reason() is None
                else str(pipeline.get_failure_reason())
            ),
            wall_seconds=wall_seconds,
            jobs=jobs or [],
            test_names=test_names,
            columns=columns,
        )

    @property
    def match_text(self) -> str:
        return "match" if self.matched else "mismatch"

    def rows(self) -> list[tuple[str, list[TestRowView | None]]]:
        return [
            (name, [column.test(index) for column in self.columns])
            for index, name in enumerate(self.test_names)
        ]

    def _repr_html_(self) -> str:
        from olympdev.frontend.html import render_verification_report

        return render_verification_report(self)


def _group_id_by_test_name(configuration: ProblemConfiguration | None) -> dict[str, int | None]:
    if configuration is None:
        return {}
    return {
        test.display_name(): test.group_id
        for test in (*configuration.samples, *configuration.tests)
    }


def _merge_reason(existing: str | None, addition: str) -> str:
    return addition if existing is None else f"{existing}; {addition}"


class StatementResultView(FrozenModel):
    """One statement build: its outcome and the PDF's digest.

    Unlike ``PipelineRunView``, there is no per-test result to unpack from
    storage - ``StatementPipeline.complete()`` already makes the pipeline's
    own output digest the compiled PDF's digest.
    """

    status: PipelineStatus
    problem_name: str | None = None
    lang: str | None = None
    failure_reason: str | None = None
    wall_seconds: float | None = None
    pdf_digest: str | None = None
    #: TeX's .log and the compiler's streams. On a failed build these are the
    #: whole diagnosis, and a failed build has no PDF digest to print instead.
    log_digest: str | None = None
    stdout_digest: str | None = None
    stderr_digest: str | None = None
    jobs: list[JobView] = []

    @classmethod
    def from_pipeline(
        cls,
        pipeline: AbstractPipeline,
        storage: AbstractStorage | None = None,
        *,
        problem_name: str | None = None,
        lang: str | None = None,
        wall_seconds: float | None = None,
        jobs: list[JobView] | None = None,
    ) -> Self:
        compile_result = None
        if storage is not None and isinstance(pipeline, StatementPipeline):
            compile_result = pipeline.compile_result(storage)
        return cls(
            status=pipeline.get_status(),
            problem_name=problem_name,
            lang=lang,
            log_digest=None if compile_result is None else compile_result.log_digest,
            stdout_digest=(
                None if compile_result is None else compile_result.stdout_digest
            ),
            stderr_digest=(
                None if compile_result is None else compile_result.stderr_digest
            ),
            failure_reason=(
                None
                if pipeline.get_failure_reason() is None
                else str(pipeline.get_failure_reason())
            ),
            wall_seconds=wall_seconds,
            pdf_digest=pipeline.get_output_digest(),
            jobs=jobs or [],
        )

    def _repr_html_(self) -> str:
        from olympdev.frontend.html import render_statement_result

        return render_statement_result(self)


class StressResultView(FrozenModel):
    """One stress run: whether a counterexample turned up, and where."""

    status: PipelineStatus
    problem_name: str | None = None
    failure_reason: str | None = None
    wall_seconds: float | None = None
    found: bool = False
    iterations_run: int = 0
    seed_argv: list[str] = []
    verdict: Verdict | None = None
    input_digest: str | None = None
    candidate_run_digest: str | None = None
    reference_run_digest: str | None = None
    check_digest: str | None = None
    jobs: list[JobView] = []

    @classmethod
    def from_pipeline(
        cls,
        pipeline: AbstractPipeline,
        storage: AbstractStorage,
        *,
        problem_name: str | None = None,
        wall_seconds: float | None = None,
        jobs: list[JobView] | None = None,
    ) -> Self:
        digest = pipeline.get_output_digest()
        result = StressResult.from_storage(storage, digest) if digest is not None else None
        return cls(
            status=pipeline.get_status(),
            problem_name=problem_name,
            failure_reason=(
                None
                if pipeline.get_failure_reason() is None
                else str(pipeline.get_failure_reason())
            ),
            wall_seconds=wall_seconds,
            found=False if result is None else result.found,
            iterations_run=0 if result is None else result.iterations_run,
            seed_argv=(
                [] if result is None or result.seed_argv is None else result.seed_argv
            ),
            verdict=None if result is None else result.verdict,
            input_digest=None if result is None else result.input_digest,
            candidate_run_digest=None if result is None else result.candidate_run_digest,
            reference_run_digest=None if result is None else result.reference_run_digest,
            check_digest=None if result is None else result.check_digest,
            jobs=jobs or [],
        )

    @property
    def verdict_text(self) -> str:
        return "—" if self.verdict is None else self.verdict.value

    def _repr_html_(self) -> str:
        from olympdev.frontend.html import render_stress_result

        return render_stress_result(self)


def _verification_result(
    pipeline: AbstractPipeline, storage: AbstractStorage
) -> ProblemVerificationResult | None:
    digest = pipeline.get_output_digest()
    if digest is None:
        return None
    return ProblemVerificationResult.from_storage(storage, digest)
