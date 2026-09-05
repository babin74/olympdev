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
from olympdev.schema import FrozenModel
from olympdev.storage import AbstractStorage
from olympdev.worker.job import AbstractJob, JobStatus
from olympdev.worker.pipeline import AbstractPipeline, PipelineStatus
from olympdev.worker.pipelines.problem_test import ProblemTestResult
from olympdev.worker.pipelines.problem_testing import ProblemTestingResult
from olympdev.worker.verdict import Verdict

if TYPE_CHECKING:
    from olympdev.worker.jobs.sandbox import SandboxJobResult


def format_duration(milliseconds: float | None) -> str:
    if milliseconds is None:
        return "—"
    if milliseconds < 10:
        return f"{milliseconds:.1f} ms"
    return f"{milliseconds:.0f} ms"


def format_memory(kilobytes: int | None) -> str:
    if kilobytes is None:
        return "—"
    if kilobytes >= 1024:
        return f"{kilobytes / 1024:.1f} MB"
    return f"{kilobytes} KB"


class UsageView(FrozenModel):
    """Time and memory a run actually used."""

    elapsed_ms: float | None = None
    memory_kb: int | None = None

    @classmethod
    def from_meta(cls, meta: IsolateMeta) -> Self:
        # isolate's `time` is CPU time. A solution that sleeps hits the wall
        # clock limit having used almost no CPU, so a timeout reported as CPU
        # time would read as a few milliseconds.
        seconds = meta.time_wall if meta.status == "TO" else meta.time
        return cls(
            elapsed_ms=seconds * 1000.0,
            memory_kb=meta.cg_memory_kb or meta.max_rss_kb or None,
        )

    @property
    def elapsed_text(self) -> str:
        return format_duration(self.elapsed_ms)

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

    @classmethod
    def from_result(
        cls, storage: AbstractStorage, result: ProblemTestResult
    ) -> Self:
        return cls(
            name=result.name,
            verdict=result.verdict,
            usage=_run_usage(storage, result.run_digest),
            run_digest=result.run_digest,
            check_digest=result.check_digest,
        )


def _run_usage(storage: AbstractStorage, run_digest: str | None) -> UsageView:
    if run_digest is None:
        return EMPTY_USAGE
    from olympdev.worker.jobs.sandbox import SandboxJobResult

    result: SandboxJobResult | None = SandboxJobResult.from_storage(
        storage, run_digest
    )
    if result is None:
        return EMPTY_USAGE
    return UsageView.from_meta(result.meta)


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
    def wall_text(self) -> str:
        if self.wall_seconds is None:
            return "—"
        return format_duration(self.wall_seconds * 1000.0)

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


class SolutionColumnView(FrozenModel):
    """One solution's results, as a column of the comparison matrix."""

    name: str
    tag: str | None = None
    verdict: Verdict | None = None
    wall_seconds: float | None = None
    tests: list[TestRowView] = []

    @property
    def verdict_text(self) -> str:
        return "—" if self.verdict is None else self.verdict.value

    @property
    def wall_text(self) -> str:
        if self.wall_seconds is None:
            return "—"
        return format_duration(self.wall_seconds * 1000.0)

    def test(self, index: int) -> TestRowView | None:
        return self.tests[index] if index < len(self.tests) else None


class TestingReportView(FrozenModel):
    """Several solutions over the same tests: the test x solution matrix."""

    problem_name: str | None = None
    test_names: list[str] = []
    columns: list[SolutionColumnView] = []

    @classmethod
    def from_columns(
        cls,
        columns: list[SolutionColumnView],
        *,
        problem_name: str | None = None,
    ) -> Self:
        names: list[str] = []
        for column in columns:
            if len(column.tests) > len(names):
                names = [test.name for test in column.tests]
        return cls(problem_name=problem_name, test_names=names, columns=columns)

    def rows(self) -> list[tuple[str, list[TestRowView | None]]]:
        """Each test with one cell per solution; None where a run has no result."""
        return [
            (name, [column.test(index) for column in self.columns])
            for index, name in enumerate(self.test_names)
        ]

    def _repr_html_(self) -> str:
        from olympdev.frontend.html import render_testing_report

        return render_testing_report(self)


def solution_column(
    pipeline: AbstractPipeline,
    storage: AbstractStorage,
    *,
    name: str,
    tag: str | None = None,
    wall_seconds: float | None = None,
) -> SolutionColumnView:
    result = _testing_result(pipeline, storage)
    return SolutionColumnView(
        name=name,
        tag=tag,
        verdict=None if result is None else result.verdict,
        wall_seconds=wall_seconds,
        tests=(
            []
            if result is None
            else [TestRowView.from_result(storage, test) for test in result.tests]
        ),
    )
