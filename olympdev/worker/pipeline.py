from abc import ABC, abstractmethod
from enum import Enum, auto
from typing import Any, ClassVar, Optional, Self, TypeVar

from olympdev.isolate import IsolateFactory
from olympdev.schema import FrozenModel, FrozenRuntimeModel, StorageModel
from olympdev.storage import AbstractStorage
from olympdev.worker.executor import JobExecutor
from olympdev.worker.job import AbstractJob, JobContext, JobStatus, resolve_job_class

_IN_FLIGHT = (JobStatus.PENDING, JobStatus.RUNNING)
_TResult = TypeVar("_TResult", bound=StorageModel)

_PIPELINE_REGISTRY: dict[str, type["AbstractPipeline"]] = {}


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


class StepState(FrozenModel):
    """One issued job, in a form that survives a restart."""

    job_id: str
    job_type: str
    input_digest: str
    status: JobStatus
    output_digest: str | None = None
    failure_reason: str | None = None
    labels: dict[str, str] = {}


class PipelineState(StorageModel):
    """A pipeline's whole progress: its own steps plus its children.

    Everything a pipeline needs to keep deciding is here, so a worker can be
    restarted (or a run handed to another worker) without losing work: jobs
    already finished are restored with their results and are not re-run.
    """

    pipeline_type: str
    pipeline_id: str
    input_digest: str
    status: PipelineStatus
    result: str | None = None
    failure_reason: str | None = None
    steps: dict[str, StepState] = {}
    children: dict[str, "PipelineState"] = {}


class AbstractPipeline(ABC):
    """Stateful orchestrator for one process made of several jobs.

    The pipeline does not run isolate and does not call ``job.execute``.
    It only decides which job is runnable given results of jobs it already
    issued. Job input digests of later steps are built from earlier
    ``output_digest`` values (and storage).

    All progress lives in the step table and the child registry, both of
    which serialize. Subclasses must not keep decision state anywhere else;
    anything else they hold must be a cache re-derivable from storage.
    """

    #: Stable name used to rebuild this pipeline from a stored state.
    pipeline_type: ClassVar[str]

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        pipeline_type = cls.__dict__.get("pipeline_type")
        if pipeline_type is None:
            return
        existing = _PIPELINE_REGISTRY.get(pipeline_type)
        if existing is not None and existing is not cls:
            raise TypeError(
                f"duplicate pipeline_type {pipeline_type!r}: "
                f"{existing.__name__} and {cls.__name__}"
            )
        _PIPELINE_REGISTRY[pipeline_type] = cls

    def __init__(self, pipeline_id: str):
        self.pipeline_id = pipeline_id
        self.pipeline_status: PipelineStatus = PipelineStatus.PENDING
        self.pipeline_failure_reason: Optional[Any] = None
        self.pipeline_result: Optional[str] = None
        self._steps: dict[str, ScheduledJob] = {}
        self._children: dict[str, "ScheduledPipeline"] = {}

    # --- step table -----------------------------------------------------

    def issue(
        self,
        name: str,
        job_cls: type[AbstractJob],
        input_digest: str,
        *,
        job_id: str | None = None,
        labels: dict[str, str] | None = None,
    ) -> ScheduledJob:
        """Record and return a job for the runner to execute."""
        if name in self._steps:
            raise RuntimeError(f"step {name!r} was already issued")
        scheduled = ScheduledJob(
            job=job_cls(job_id or name, labels=labels),
            input_digest=input_digest,
        )
        self._steps[name] = scheduled
        return scheduled

    def step(self, name: str) -> ScheduledJob | None:
        return self._steps.get(name)

    def has_step(self, name: str) -> bool:
        return name in self._steps

    def step_in_flight(self, name: str) -> bool:
        scheduled = self._steps.get(name)
        return scheduled is not None and scheduled.job.get_status() in _IN_FLIGHT

    def any_step_in_flight(self, *names: str) -> bool:
        names = names or tuple(self._steps)
        return any(self.step_in_flight(name) for name in names)

    def step_result(
        self,
        name: str,
        result_type: type[_TResult],
        storage: AbstractStorage,
        *,
        missing: str,
    ) -> tuple[_TResult, str] | tuple[None, None]:
        """Load a finished step's result, failing this pipeline if it is absent."""
        scheduled = self._steps[name]
        if scheduled.job.get_status() is JobStatus.FAILED:
            self.fail(scheduled.job.get_failure_reason())
            return None, None
        digest = scheduled.job.get_output_digest()
        result = (
            result_type.from_storage(storage, digest) if digest is not None else None
        )
        if result is None or digest is None:
            self.fail(missing)
            return None, None
        return result, digest

    # --- children -------------------------------------------------------

    def add_child(
        self, name: str, pipeline: "AbstractPipeline", input_digest: str
    ) -> "ScheduledPipeline":
        scheduled = ScheduledPipeline(pipeline=pipeline, input_digest=input_digest)
        self._children[name] = scheduled
        return scheduled

    def children(self) -> list["ScheduledPipeline"]:
        return list(self._children.values())

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

    # --- driving --------------------------------------------------------

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

    # --- serialization --------------------------------------------------

    def save_state(self, input_digest: str) -> PipelineState:
        """Snapshot this pipeline (and its children) for storage or transfer."""
        return PipelineState(
            pipeline_type=type(self).pipeline_type,
            pipeline_id=self.pipeline_id,
            input_digest=input_digest,
            status=self.pipeline_status,
            result=self.pipeline_result,
            failure_reason=(
                None
                if self.pipeline_failure_reason is None
                else str(self.pipeline_failure_reason)
            ),
            steps={
                name: StepState(
                    job_id=scheduled.job.job_id,
                    job_type=type(scheduled.job).job_type,
                    input_digest=scheduled.input_digest,
                    status=scheduled.job.get_status(),
                    output_digest=scheduled.job.get_output_digest(),
                    failure_reason=(
                        None
                        if scheduled.job.get_failure_reason() is None
                        else str(scheduled.job.get_failure_reason())
                    ),
                    labels=dict(scheduled.job.labels),
                )
                for name, scheduled in self._steps.items()
            },
            children={
                name: child.pipeline.save_state(child.input_digest)
                for name, child in self._children.items()
            },
        )

    @classmethod
    def restore(cls, state: PipelineState, storage: AbstractStorage) -> Self:
        """Rebuild a pipeline from a snapshot, finished jobs included."""
        pipeline_cls = _PIPELINE_REGISTRY[state.pipeline_type]
        pipeline = pipeline_cls(state.pipeline_id)
        pipeline.pipeline_status = state.status
        pipeline.pipeline_result = state.result
        pipeline.pipeline_failure_reason = state.failure_reason
        for name, step in state.steps.items():
            job_cls = resolve_job_class(storage, step.input_digest)
            if job_cls.job_type != step.job_type:
                raise ValueError(
                    f"step {name!r} job_type changed: stored {step.job_type!r}, "
                    f"input resolves to {job_cls.job_type!r}"
                )
            job = job_cls(step.job_id, labels=step.labels)
            job.job_status = step.status
            job.job_result = step.output_digest
            job.job_failure_reason = step.failure_reason
            pipeline._steps[name] = ScheduledJob(
                job=job, input_digest=step.input_digest
            )
        for name, child_state in state.children.items():
            child = AbstractPipeline.restore(child_state, storage)
            pipeline._children[name] = ScheduledPipeline(
                pipeline=child, input_digest=child_state.input_digest
            )
        return pipeline  # type: ignore[return-value]


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
    observer: "PipelineObserver | None" = None,
) -> None:
    """Sequential driver: execute each issued job before polling again.

    ``NextJobStatus.WAITING`` is a deadlock here (no in-flight jobs).
    Parallel runners should pull until WAITING, keep jobs in flight, and
    poll again after any of them finishes.
    """
    from olympdev.worker.observer import NULL_OBSERVER

    if executor is None:
        executor = JobExecutor()
    if observer is None:
        observer = NULL_OBSERVER
    while True:
        nxt = pipeline.get_next_job(context)
        if nxt is NextJobStatus.FINISHED:
            observer.on_pipeline_finished(pipeline)
            return
        if nxt is NextJobStatus.WAITING:
            raise RuntimeError(
                f"pipeline {pipeline.pipeline_id!r} is waiting with no in-flight jobs"
            )
        observer.on_job_issued(nxt.job, nxt.input_digest)
        executor.execute(nxt.job, context.job_context(nxt.input_digest))
        observer.on_job_finished(nxt.job)
