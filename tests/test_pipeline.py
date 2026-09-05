from olympdev.storage.filestorage import FileStorage
from olympdev.worker.cache.policy import CachePolicy
from olympdev.worker.job import AbstractJob, JobContext, JobStatus
from olympdev.worker.pipeline import (
    AbstractPipeline,
    NextJobStatus,
    PipelineContext,
    PipelineStatus,
    ScheduledJob,
    run_pipeline,
)


class EchoJob(AbstractJob):
    job_type = "echo"

    def get_description(self) -> str:
        return "Echo payload"

    def get_cache_policy(self) -> CachePolicy:
        return CachePolicy.Never

    @classmethod
    def make_input_digest(cls, storage: FileStorage, *, payload: str) -> str:
        return cls._upload_input(storage, {"payload": payload})

    def _parse_arguments(self, context: JobContext) -> dict:
        arguments = context.storage.download_dictionary(context.input_digest)
        if arguments is None:
            raise ValueError("missing echo job input")
        return self._require_job_type(arguments)

    def _execute(self, context: JobContext) -> str | None:
        arguments = self._parse_arguments(context)
        if arguments["payload"] == "boom":
            self.fail("echo failed")
            return None
        return context.storage.upload_dictionary({"echo": arguments["payload"]})


class EchoThenPrefixPipeline(AbstractPipeline):
    def __init__(self, pipeline_id: str):
        super().__init__(pipeline_id)
        self._first: ScheduledJob | None = None
        self._second: ScheduledJob | None = None

    @classmethod
    def make_input_digest(cls, storage: FileStorage, *, payload: str) -> str:
        return storage.upload_dictionary({"payload": payload})

    def _payload(self, context: PipelineContext) -> str:
        return context.storage.download_dictionary(context.input_digest)["payload"]

    def _next_job(self, context: PipelineContext) -> ScheduledJob | NextJobStatus:
        storage = context.storage
        if self._first is None:
            job = EchoJob("first")
            digest = EchoJob.make_input_digest(storage, payload=self._payload(context))
            self._first = ScheduledJob(job=job, input_digest=digest)
            return self._first

        first_status = self._first.job.get_status()
        if first_status in (JobStatus.PENDING, JobStatus.RUNNING):
            return NextJobStatus.WAITING
        if first_status is JobStatus.FAILED:
            self.fail(self._first.job.get_failure_reason())
            return NextJobStatus.FINISHED

        if self._second is None:
            first_result = storage.download_dictionary(
                self._first.job.get_output_digest()
            )
            job = EchoJob("second")
            digest = EchoJob.make_input_digest(
                storage,
                payload="x" + first_result["echo"],
            )
            self._second = ScheduledJob(job=job, input_digest=digest)
            return self._second

        second_status = self._second.job.get_status()
        if second_status in (JobStatus.PENDING, JobStatus.RUNNING):
            return NextJobStatus.WAITING
        if second_status is JobStatus.FAILED:
            self.fail(self._second.job.get_failure_reason())
            return NextJobStatus.FINISHED

        self.complete(self._second.job.get_output_digest())
        return NextJobStatus.FINISHED


def _pipeline_context(storage: FileStorage, payload: str) -> PipelineContext:
    return PipelineContext(
        input_digest=EchoThenPrefixPipeline.make_input_digest(
            storage, payload=payload
        ),
        storage=storage,
    )


def test_run_pipeline_chains_job_outputs(storage: FileStorage):
    pipeline = EchoThenPrefixPipeline("echo-prefix")
    context = _pipeline_context(storage, "hello")

    run_pipeline(pipeline, context)

    assert pipeline.get_status() is PipelineStatus.COMPLETED
    result = storage.download_dictionary(pipeline.get_output_digest())
    assert result == {"echo": "xhello"}


def test_pipeline_fails_when_first_job_fails(storage: FileStorage):
    pipeline = EchoThenPrefixPipeline("echo-fail")
    context = _pipeline_context(storage, "boom")

    run_pipeline(pipeline, context)

    assert pipeline.get_status() is PipelineStatus.FAILED
    assert pipeline.get_failure_reason() == "echo failed"
    assert pipeline.get_output_digest() is None
    assert pipeline._second is None


def test_get_next_job_waiting_until_issued_job_runs(storage: FileStorage):
    pipeline = EchoThenPrefixPipeline("echo-wait")
    context = _pipeline_context(storage, "hello")

    first = pipeline.get_next_job(context)
    assert isinstance(first, ScheduledJob)
    assert pipeline.get_next_job(context) is NextJobStatus.WAITING

    first.job.execute(context.job_context(first.input_digest))

    second = pipeline.get_next_job(context)
    assert isinstance(second, ScheduledJob)
    assert second.job.job_id == "second"


def test_finished_pipeline_keeps_returning_finished(storage: FileStorage):
    pipeline = EchoThenPrefixPipeline("echo-done")
    context = _pipeline_context(storage, "hello")
    run_pipeline(pipeline, context)

    assert pipeline.get_next_job(context) is NextJobStatus.FINISHED
    assert pipeline.get_next_job(context) is NextJobStatus.FINISHED


def test_next_job_finished_without_complete_raises(storage: FileStorage):
    class BrokenPipeline(AbstractPipeline):
        @classmethod
        def make_input_digest(cls, storage: FileStorage, *args, **kwargs) -> str:
            return storage.upload_dictionary({})

        def _next_job(self, context: PipelineContext) -> ScheduledJob | NextJobStatus:
            return NextJobStatus.FINISHED

    pipeline = BrokenPipeline("broken")
    context = PipelineContext(
        input_digest=BrokenPipeline.make_input_digest(storage),
        storage=storage,
    )

    try:
        pipeline.get_next_job(context)
    except RuntimeError as exc:
        assert "without complete() or fail()" in str(exc)
    else:
        raise AssertionError("expected RuntimeError")


def test_pipeline_context_job_context_copies_runtime(storage: FileStorage):
    context = PipelineContext(
        input_digest="a" * 64,
        storage=storage,
        isolate_factory=None,
    )
    job_context = context.job_context("b" * 64)

    assert job_context.input_digest == "b" * 64
    assert job_context.storage is storage
    assert job_context.isolate_factory is None

    child = context.with_input("c" * 64)
    assert child.input_digest == "c" * 64
    assert child.storage is storage
    assert child.isolate_factory is None


def test_execute_exception_marks_job_failed(storage: FileStorage):
    class BoomJob(EchoJob):
        def _execute(self, context: JobContext) -> str | None:
            raise RuntimeError("boom")

    job = BoomJob("boom")
    job.execute(JobContext(input_digest=EchoJob.make_input_digest(storage, payload="x"), storage=storage))
    assert job.get_status() is JobStatus.FAILED
    assert "boom" in str(job.get_failure_reason())
