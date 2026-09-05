from abc import ABC, abstractmethod
from enum import Enum, auto
from typing import Any, Optional

from olympdev.isolate import IsolateFactory
from olympdev.schema import FrozenRuntimeModel
from olympdev.storage import AbstractStorage
from olympdev.worker.executor import JobExecutor
from olympdev.worker.job import AbstractJob, JobContext


class PipelineStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class NextJobStatus(Enum):
    """Returned by ``get_next_job`` when no job is handed out.

    ``WAITING`` — there is more work, but it depends on jobs already issued
    (their ``job_status`` is still ``PENDING`` / ``RUNNING``). A sequential
    runner that executes each job before polling again should never see this.
    A parallel runner treats it as "drain in-flight jobs, then poll again".

    ``FINISHED`` — the pipeline called ``complete`` or ``fail``; no further
    jobs will be issued.
    """

    FINISHED = auto()
    WAITING = auto()


class PipelineContext(FrozenRuntimeModel):
    """Runtime for one pipeline run. Same shape as ``JobContext``.

    ``input_digest`` is the pipeline's own input in storage (not a job's).
    ``isolate_factory`` is for the runner: it is copied onto each
    ``JobContext``. The pipeline itself must not open sandboxes.
    """

    input_digest: str
    storage: AbstractStorage
    isolate_factory: IsolateFactory | None = None

    def job_context(self, input_digest: str) -> JobContext:
        return JobContext(
            input_digest=input_digest,
            storage=self.storage,
            isolate_factory=self.isolate_factory,
        )

    def with_input(self, input_digest: str) -> "PipelineContext":
        return PipelineContext(
            input_digest=input_digest,
            storage=self.storage,
            isolate_factory=self.isolate_factory,
        )


class ScheduledJob(FrozenRuntimeModel):
    """A job the runner must execute (or complete from cache) in place.

    ``input_digest`` is the cache key and the ``JobContext.input_digest``.
    The pipeline keeps this ``job`` instance and reads its status/result on
    the next ``get_next_job`` call — the runner must mutate the same object.
    """

    job: AbstractJob
    input_digest: str


class AbstractPipeline(ABC):
    """Stateful orchestrator for one process made of several jobs.

    The pipeline does not run isolate and does not call ``job.execute``.
    It only decides which job is runnable given results of jobs it already
    issued. Job input digests of later steps are built from earlier
    ``output_digest`` values (and storage).
    """

    def __init__(self, pipeline_id: str):
        self.pipeline_id = pipeline_id
        self.pipeline_status: PipelineStatus = PipelineStatus.PENDING
        self.pipeline_failure_reason: Optional[Any] = None
        self.pipeline_result: Optional[str] = None

    def get_next_job(self, context: PipelineContext) -> ScheduledJob | NextJobStatus:
        if self.is_finished():
            return NextJobStatus.FINISHED
        if self.pipeline_status is PipelineStatus.PENDING:
            self.pipeline_status = PipelineStatus.RUNNING
        nxt = self._next_job(context)
        if nxt is NextJobStatus.FINISHED and not self.is_finished():
            raise RuntimeError(
                f"pipeline {self.pipeline_id!r} returned FINISHED "
                "without complete() or fail()"
            )
        return nxt

    @classmethod
    @abstractmethod
    def make_input_digest(cls, storage: AbstractStorage, *args, **kwargs) -> str:
        """Upload pipeline inputs and return the digest of the input dictionary."""

    @abstractmethod
    def _next_job(self, context: PipelineContext) -> ScheduledJob | NextJobStatus:
        """Issue at most one ready job, or WAITING / FINISHED.

        Before returning ``FINISHED``, call ``complete`` or ``fail``.
        Do not return the same job instance twice.
        """

    def complete(self, output_digest: str | None) -> None:
        if self.pipeline_status is PipelineStatus.FAILED:
            return
        self.pipeline_status = PipelineStatus.COMPLETED
        self.pipeline_result = output_digest

    def fail(self, failure_reason: Any) -> None:
        self.pipeline_status = PipelineStatus.FAILED
        self.pipeline_failure_reason = failure_reason

    def is_finished(self) -> bool:
        return self.pipeline_status in (PipelineStatus.COMPLETED, PipelineStatus.FAILED)

    def get_status(self) -> PipelineStatus:
        return self.pipeline_status

    def get_failure_reason(self) -> Optional[Any]:
        return self.pipeline_failure_reason

    def get_output_digest(self) -> Optional[str]:
        return self.pipeline_result

    def pull_child(
        self, child: "ScheduledPipeline", context: PipelineContext
    ) -> ScheduledJob | None:
        """Advance a nested pipeline. Returns a job the runner must execute.

        ``None`` means the child finished this step without issuing work.
        If the child failed, this pipeline is failed too.
        """
        nxt = child.get_next_job(context)
        if isinstance(nxt, ScheduledJob):
            return nxt
        if child.pipeline.get_status() is PipelineStatus.FAILED:
            self.fail(child.pipeline.get_failure_reason())
        return None


class ScheduledPipeline(FrozenRuntimeModel):
    """A nested pipeline the parent owns, analogous to ``ScheduledJob``.

    ``input_digest`` is the child's ``PipelineContext.input_digest``.
    The parent keeps this ``pipeline`` instance and polls it — later jobs
    are issued from the same object.
    """

    pipeline: AbstractPipeline
    input_digest: str

    def get_next_job(
        self, parent_context: PipelineContext
    ) -> ScheduledJob | NextJobStatus:
        return self.pipeline.get_next_job(parent_context.with_input(self.input_digest))


def run_pipeline(
    pipeline: AbstractPipeline,
    context: PipelineContext,
    *,
    executor: JobExecutor | None = None,
) -> None:
    """Sequential driver: execute each issued job before polling again.

    ``NextJobStatus.WAITING`` is a deadlock here (no in-flight jobs).
    Parallel runners should pull until WAITING, keep jobs in flight, and
    poll again after any of them finishes.
    """

    if executor is None:
        executor = JobExecutor()
    while True:
        nxt = pipeline.get_next_job(context)
        if nxt is NextJobStatus.FINISHED:
            return
        if nxt is NextJobStatus.WAITING:
            raise RuntimeError(
                f"pipeline {pipeline.pipeline_id!r} is waiting with no in-flight jobs"
            )
        executor.execute(nxt.job, context.job_context(nxt.input_digest))
