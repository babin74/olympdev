"""Both drivers must agree: same results, same observer events, same executor.

The notebook used to carry a hand-copied run loop that had silently drifted
from run_pipeline (it bypassed JobExecutor, so caching never applied). These
tests are what makes copying a driver unnecessary - and what would catch a
drift like that.
"""

from pathlib import Path

import pytest

from olympdev.isolate import IsolateFactory
from olympdev.storage.filestorage import FileStorage
from olympdev.worker.cache.storage import MemoryCacheStorage
from olympdev.worker.executor import JobExecutor
from olympdev.worker.job import JobStatus
from olympdev.worker.observer import NullObserver, TextObserver
from olympdev.worker.pipeline import PipelineContext, PipelineStatus, run_pipeline
from olympdev.worker.pipelines.problem_testing import ProblemTestingPipeline
from olympdev.worker.runner import run_pipeline_parallel
from olympdev.worker.verdict import Verdict

from test_problem_testing_pipeline import _configuration, _make_digest

pytestmark = pytest.mark.slow


class RecordingObserver(NullObserver):
    def __init__(self) -> None:
        self.issued: list[str] = []
        self.finished: list[str] = []
        self.pipelines: list[str] = []

    def on_job_issued(self, job, input_digest: str) -> None:
        self.issued.append(job.job_id)

    def on_job_finished(self, job) -> None:
        self.finished.append(job.job_id)

    def on_pipeline_finished(self, pipeline) -> None:
        self.pipelines.append(pipeline.pipeline_id)


class CountingExecutor(JobExecutor):
    def __init__(self, cache=None):
        super().__init__(cache)
        self.executed: list[str] = []

    def execute(self, job, context) -> None:
        super().execute(job, context)
        self.executed.append(job.job_id)


@pytest.fixture
def problem(storage: FileStorage, test_assets_path: Path, tmp_path: Path) -> str:
    extra = []
    for index in range(3):
        name = f"{index}.in"
        (tmp_path / name).write_bytes(b"")
        from olympdev.problem.types import ProblemTest

        extra.append(ProblemTest(input_file=name, output_file="sample.out"))
    configuration = _configuration(tmp_path, extra_tests=extra)
    return _make_digest(storage, test_assets_path, configuration, tmp_path)


def _context(storage, isolate_factory, digest) -> PipelineContext:
    return PipelineContext(
        input_digest=digest, storage=storage, isolate_factory=isolate_factory
    )


def test_parallel_runner_produces_the_same_result(
    storage: FileStorage, isolate_factory: IsolateFactory, problem: str
):
    sequential = ProblemTestingPipeline("sequential")
    run_pipeline(sequential, _context(storage, isolate_factory, problem))

    parallel = ProblemTestingPipeline("parallel")
    run_pipeline_parallel(
        parallel, _context(storage, IsolateFactory(3), problem), max_workers=3
    )

    assert sequential.get_status() is parallel.get_status() is PipelineStatus.COMPLETED
    left = sequential.get_result(storage)
    right = parallel.get_result(storage)
    assert left is not None and right is not None
    assert left.verdict is right.verdict is Verdict.OK
    assert [t.name for t in left.tests] == [t.name for t in right.tests]
    assert [t.verdict for t in left.tests] == [t.verdict for t in right.tests]


def test_both_drivers_report_the_same_jobs(
    storage: FileStorage, isolate_factory: IsolateFactory, problem: str
):
    sequential_observer = RecordingObserver()
    run_pipeline(
        ProblemTestingPipeline("sequential"),
        _context(storage, isolate_factory, problem),
        observer=sequential_observer,
    )

    parallel_observer = RecordingObserver()
    run_pipeline_parallel(
        ProblemTestingPipeline("parallel"),
        _context(storage, IsolateFactory(3), problem),
        observer=parallel_observer,
        max_workers=3,
    )

    # Order differs by design; the set of work does not.
    assert sorted(sequential_observer.issued) == sorted(parallel_observer.issued)
    assert sorted(sequential_observer.finished) == sorted(parallel_observer.finished)
    assert sequential_observer.issued and sequential_observer.finished
    assert sequential_observer.pipelines == ["sequential"]
    assert parallel_observer.pipelines == ["parallel"]


@pytest.mark.parametrize("driver", ["sequential", "parallel"])
def test_every_driver_goes_through_the_job_executor(
    storage: FileStorage, isolate_factory: IsolateFactory, problem: str, driver: str
):
    """A driver that called job.execute directly would skip the cache."""
    executor = CountingExecutor(MemoryCacheStorage())
    pipeline = ProblemTestingPipeline(driver)
    context = _context(storage, isolate_factory, problem)
    if driver == "sequential":
        run_pipeline(pipeline, context, executor=executor)
    else:
        run_pipeline_parallel(pipeline, context, executor=executor, max_workers=1)
    assert executor.executed
    assert pipeline.get_status() is PipelineStatus.COMPLETED


def test_shared_cache_skips_recompiling_for_a_second_solution(
    storage: FileStorage, isolate_factory: IsolateFactory, problem: str
):
    cache = MemoryCacheStorage()
    context = _context(storage, isolate_factory, problem)

    first = ProblemTestingPipeline("first")
    run_pipeline(first, context, executor=JobExecutor(cache))

    second = ProblemTestingPipeline("second")
    run_pipeline(second, context, executor=JobExecutor(cache))

    checker = second.step("compile-checker")
    assert checker is not None
    assert checker.job.get_status() is JobStatus.COMPLETED
    # Same digest as the first run: the cached outcome was reused.
    assert checker.job.get_output_digest() == (
        first.step("compile-checker").job.get_output_digest()
    )


def test_text_observer_writes_a_line_per_event():
    lines: list[str] = []
    observer = TextObserver(lines.append)

    from olympdev.worker.jobs.compile import SolutionCompileJob

    job = SolutionCompileJob("compile-solution")
    observer.on_job_issued(job, "a" * 64)
    job.job_status = JobStatus.COMPLETED
    observer.on_job_finished(job)

    assert lines == ["  -> compile-solution", "  completed compile-solution"]
