"""A small façade for driving olympdev from a notebook.

Owns the pieces a session needs - storage, a sandbox pool, an executor and a
cache - so a cell says what it wants tested rather than how to wire the
machinery. Everything it returns renders itself, so no cell needs
`display(HTML(...))` or its own HTML.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Self

from olympdev.frontend.view import (
    JobView,
    PipelineRunView,
    TestingReportView,
    solution_column,
)
from olympdev.isolate import IsolateFactory
from olympdev.problem.build import build_problem_testing_input
from olympdev.problem.package import Contest, ProblemPackage, load_target
from olympdev.settings import Settings, get_settings
from olympdev.storage import AbstractStorage, open_storage
from olympdev.worker.cache.storage import AbstractCacheStorage, MemoryCacheStorage
from olympdev.worker.executor import JobExecutor
from olympdev.worker.observer import NullObserver
from olympdev.worker.pipeline import PipelineContext, run_pipeline
from olympdev.worker.pipelines.problem_testing import ProblemTestingPipeline
from olympdev.worker.runner import run_pipeline_parallel


class NotebookObserver(NullObserver):
    """Show each job as it finishes, and collect the cards for the run view."""

    def __init__(self, *, live: bool = True):
        self.jobs: list[JobView] = []
        self._live = live

    def on_job_finished(self, job) -> None:
        view = JobView.from_job(job)
        self.jobs.append(view)
        if self._live:
            self._display(view)

    @staticmethod
    def _display(view: JobView) -> None:
        try:
            from IPython.display import display
        except ModuleNotFoundError:  # pragma: no cover - outside a notebook
            return
        display(view)


class ProblemWorkbench:
    """One problem, ready to test."""

    def __init__(self, workbench: "Workbench", package: ProblemPackage):
        self.workbench = workbench
        self.package = package

    @property
    def name(self) -> str:
        return self.package.name

    def solutions(self) -> list[str]:
        return [solution.tag for solution in self.package.solutions()]

    def input_digest(self, solution: Any = None) -> str:
        return build_problem_testing_input(
            self.workbench.storage,
            self.package,
            solution=self._resolve(solution),
            settings=self.workbench.settings,
        )

    def _resolve(self, solution: Any) -> Any:
        if isinstance(solution, str) and self.package.configuration.solution(solution):
            return self.package.solution(solution)
        return solution

    def run(
        self,
        solution: Any = None,
        *,
        trace: bool = False,
        jobs: int | None = None,
        cache: AbstractCacheStorage | None = None,
        pipeline_id: str | None = None,
    ) -> PipelineRunView:
        """Test one solution. ``trace=True`` shows each job as it finishes."""
        observer = NotebookObserver(live=trace)
        tag = solution if isinstance(solution, str) else "main"
        pipeline = ProblemTestingPipeline(pipeline_id or f"{self.name}-{tag}")
        digest = self.input_digest(solution)
        context = PipelineContext(
            input_digest=digest,
            storage=self.workbench.storage,
            isolate_factory=self.workbench.isolate_factory,
        )
        executor = JobExecutor(cache if cache is not None else self.workbench.cache)
        workers = jobs if jobs is not None else self.workbench.jobs
        started = time.perf_counter()
        if workers > 1:
            run_pipeline_parallel(
                pipeline,
                context,
                executor=executor,
                observer=observer,
                max_workers=workers,
            )
        else:
            run_pipeline(pipeline, context, executor=executor, observer=observer)
        return PipelineRunView.from_pipeline(
            pipeline,
            self.workbench.storage,
            problem_name=self.name,
            wall_seconds=time.perf_counter() - started,
            jobs=observer.jobs,
        )

    def compare(
        self,
        tags: list[str] | None = None,
        *,
        cache: bool = True,
        jobs: int | None = None,
    ) -> TestingReportView:
        """Test several solutions and lay them out as a test x solution matrix.

        With ``cache`` on, one cache is shared across solutions, so the
        checker, generator, validator and model are compiled once.
        """
        shared = MemoryCacheStorage() if cache else None
        columns = []
        for tag in tags or self.solutions():
            solution = self.package.solution(tag)
            started = time.perf_counter()
            pipeline = ProblemTestingPipeline(f"{self.name}-{tag}")
            context = PipelineContext(
                input_digest=self.input_digest(solution),
                storage=self.workbench.storage,
                isolate_factory=self.workbench.isolate_factory,
            )
            executor = JobExecutor(shared if shared is not None else self.workbench.cache)
            workers = jobs if jobs is not None else self.workbench.jobs
            if workers > 1:
                run_pipeline_parallel(
                    pipeline, context, executor=executor, max_workers=workers
                )
            else:
                run_pipeline(pipeline, context, executor=executor)
            columns.append(
                solution_column(
                    pipeline,
                    self.workbench.storage,
                    name=Path(solution.file).name,
                    tag=tag,
                    wall_seconds=time.perf_counter() - started,
                )
            )
        return TestingReportView.from_columns(columns, problem_name=self.name)


class Workbench:
    """A session: storage, sandboxes and settings, shared by every run."""

    def __init__(
        self,
        contest: Contest,
        *,
        storage: AbstractStorage,
        isolate_factory: IsolateFactory,
        jobs: int = 1,
        cache: AbstractCacheStorage | None = None,
        settings: Settings | None = None,
    ):
        self.contest = contest
        self.storage = storage
        self.isolate_factory = isolate_factory
        self.jobs = jobs
        self.cache = cache if cache is not None else MemoryCacheStorage()
        self.settings = settings or get_settings()

    @classmethod
    def open(
        cls,
        target: Path | str,
        *,
        storage: AbstractStorage | None = None,
        jobs: int | None = None,
        settings: Settings | None = None,
    ) -> Self:
        """Open a contest or a single problem, using configured defaults."""
        settings = settings or get_settings()
        if jobs is None:
            jobs = settings.isolate.sandbox_count
        return cls(
            load_target(target),
            storage=storage if storage is not None else open_storage(settings),
            isolate_factory=IsolateFactory(jobs),
            jobs=jobs,
            settings=settings,
        )

    def problems(self) -> list[str]:
        return list(self.contest.problems)

    def problem(self, name: str | None = None) -> ProblemWorkbench:
        if name is None:
            names = self.problems()
            if len(names) != 1:
                raise ValueError(f"name a problem: {', '.join(names)}")
            name = names[0]
        return ProblemWorkbench(self, self.contest.problem(name))
