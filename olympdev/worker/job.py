from abc import ABC, abstractmethod
from collections.abc import Callable
from enum import Enum
from typing import TYPE_CHECKING, Any, ClassVar, Optional

from olympdev.isolate import IsolateFactory
from olympdev.schema import FrozenRuntimeModel
from olympdev.storage import AbstractStorage
from olympdev.worker.cache.policy import CachePolicy

if TYPE_CHECKING:
    from olympdev.frontend.html.job import JobRenderContext
    from olympdev.worker.cache.storage import CachedJobOutcome

JOB_TYPE_KEY = "job_type"

_JOB_REGISTRY: dict[str, type["AbstractJob"]] = {}


class JobStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class JobContext(FrozenRuntimeModel):
    input_digest: str
    storage: AbstractStorage
    isolate_factory: IsolateFactory | None = None


_ABSTRACT_JOB_METHODS = (
    "_execute",
    "get_description",
    "get_cache_policy",
    "make_input_digest",
)


def _is_abstract_job_class(cls: type) -> bool:
    for name in _ABSTRACT_JOB_METHODS:
        value = getattr(cls, name, None)
        if getattr(value, "__isabstractmethod__", False):
            return True
    return False


class AbstractJob(ABC):
    job_type: ClassVar[str]

    def __init__(self, job_id: str, *, labels: dict[str, str] | None = None):
        self.job_id: str = job_id
        self.job_status: JobStatus = JobStatus.PENDING
        self.job_failure_reason: Optional[Any] = None
        self.job_result: Optional[str] = None
        self.labels: dict[str, str] = dict(labels or {})

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if _is_abstract_job_class(cls):
            return
        declared = "job_type" in cls.__dict__
        job_type = getattr(cls, "job_type", None)
        if not job_type:
            raise TypeError(f"{cls.__name__} must define a non-empty job_type")
        if not declared:
            return
        existing = _JOB_REGISTRY.get(job_type)
        if existing is not None and existing is not cls:
            raise TypeError(
                f"duplicate job_type {job_type!r}: "
                f"{existing.__name__} and {cls.__name__}"
            )
        _JOB_REGISTRY[job_type] = cls

    @abstractmethod
    def _execute(self, context: JobContext) -> str | None: ...

    @abstractmethod
    def get_description(self) -> str: ...

    @abstractmethod
    def get_cache_policy(self) -> CachePolicy: ...

    @classmethod
    @abstractmethod
    def make_input_digest(cls, storage: AbstractStorage, *args, **kwargs) -> str: ...

    @classmethod
    def _upload_input(cls, storage: AbstractStorage, payload: dict[str, Any]) -> str:
        if JOB_TYPE_KEY in payload:
            raise ValueError("job_type is reserved")
        return storage.upload_dictionary({JOB_TYPE_KEY: cls.job_type, **payload})

    @classmethod
    def _require_job_type(cls, arguments: dict[str, Any]) -> dict[str, Any]:
        actual = arguments.get(JOB_TYPE_KEY)
        if actual != cls.job_type:
            raise ValueError(
                f"job_type mismatch: expected {cls.job_type!r}, got {actual!r}"
            )
        return arguments

    def describe(self, render_job: Callable[["JobRenderContext"], str]) -> str:
        from olympdev.frontend.html.job import JobRenderContext

        return render_job(
            JobRenderContext(
                job_id=self.job_id,
                job_name=type(self).__name__,
                job_description=self.get_description(),
                job_status=self.job_status,
                job_result=self.job_result or "",
                job_digests=[],
                job_labels=list(self.labels.items()),
            )
        )

    def fail(self, failure_reason: Any) -> None:
        self.job_status = JobStatus.FAILED
        self.job_failure_reason = failure_reason

    def execute(self, context: JobContext) -> None:
        self.job_status = JobStatus.RUNNING
        try:
            result = self._execute(context)
        except Exception as exc:
            if self.job_status != JobStatus.FAILED:
                self.fail(f"{type(exc).__name__}: {exc}")
            return
        if self.job_status != JobStatus.FAILED:
            self.job_status = JobStatus.COMPLETED
            self.job_result = result

    def complete_from_cache(self, outcome: "CachedJobOutcome") -> None:
        self.job_status = outcome.status
        self.job_result = outcome.output_digest
        self.job_failure_reason = outcome.failure_reason

    def get_status(self) -> JobStatus:
        return self.job_status

    def get_failure_reason(self) -> Optional[Any]:
        return self.job_failure_reason

    def get_output_digest(self) -> Optional[str]:
        return self.job_result


def resolve_job_class(storage: AbstractStorage, input_digest: str) -> type[AbstractJob]:
    from olympdev.worker.jobs import ensure_jobs_registered

    ensure_jobs_registered()
    data = storage.download_dictionary(input_digest)
    if data is None:
        raise ValueError("missing job input")
    job_type = data.get(JOB_TYPE_KEY)
    if not isinstance(job_type, str) or not job_type:
        raise ValueError("missing job_type")
    try:
        return _JOB_REGISTRY[job_type]
    except KeyError as exc:
        raise ValueError(f"unknown job_type: {job_type!r}") from exc
