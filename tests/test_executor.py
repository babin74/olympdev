from olympdev.storage.filestorage import FileStorage
from olympdev.worker.cache import CachePolicy, MemoryCacheStorage
from olympdev.worker.executor import JobExecutor
from olympdev.worker.job import AbstractJob, JobContext, JobStatus


class _CountingJob(AbstractJob):
    job_type = "counting"
    executes = 0

    def get_description(self) -> str:
        return "Count executes"

    def get_cache_policy(self) -> CachePolicy:
        return CachePolicy.Validator

    @classmethod
    def make_input_digest(cls, storage: FileStorage, *, payload: str) -> str:
        return cls._upload_input(storage, {"payload": payload})

    def _execute(self, context: JobContext) -> str | None:
        type(self).executes += 1
        arguments = self._require_job_type(
            context.storage.download_dictionary(context.input_digest)
        )
        return context.storage.upload_dictionary({"echo": arguments["payload"]})


class _NeverJob(_CountingJob):
    job_type = "counting_never"

    def get_cache_policy(self) -> CachePolicy:
        return CachePolicy.Never


class _DomainFailJob(AbstractJob):
    job_type = "domain_fail"

    def get_description(self) -> str:
        return "Fail with a stored result"

    def get_cache_policy(self) -> CachePolicy:
        return CachePolicy.SolutionCompile

    @classmethod
    def make_input_digest(cls, storage: FileStorage, *, payload: str) -> str:
        return cls._upload_input(storage, {"payload": payload})

    def _execute(self, context: JobContext) -> str | None:
        self.job_result = context.storage.upload_dictionary({"failed": True})
        self.job_status = JobStatus.FAILED
        self.job_failure_reason = "Compilation failed"
        return None


class _BareFailJob(AbstractJob):
    job_type = "bare_fail"

    def get_description(self) -> str:
        return "Fail without a result"

    def get_cache_policy(self) -> CachePolicy:
        return CachePolicy.Validator

    @classmethod
    def make_input_digest(cls, storage: FileStorage, *, payload: str) -> str:
        return cls._upload_input(storage, {"payload": payload})

    def _execute(self, context: JobContext) -> str | None:
        self.fail("boom")
        return None


def test_executor_miss_then_hit(storage: FileStorage):
    _CountingJob.executes = 0
    cache = MemoryCacheStorage()
    executor = JobExecutor(cache)
    digest = _CountingJob.make_input_digest(storage, payload="hello")
    context = JobContext(input_digest=digest, storage=storage)

    first = _CountingJob("one")
    executor.execute(first, context)
    assert first.get_status() is JobStatus.COMPLETED
    assert _CountingJob.executes == 1
    output = first.get_output_digest()
    assert output is not None

    second = _CountingJob("two")
    executor.execute(second, context)
    assert second.get_status() is JobStatus.COMPLETED
    assert second.get_output_digest() == output
    assert _CountingJob.executes == 1


def test_executor_never_always_runs(storage: FileStorage):
    _NeverJob.executes = 0
    executor = JobExecutor(MemoryCacheStorage())
    digest = _NeverJob.make_input_digest(storage, payload="hello")
    context = JobContext(input_digest=digest, storage=storage)

    executor.execute(_NeverJob("one"), context)
    executor.execute(_NeverJob("two"), context)
    assert _NeverJob.executes == 2


def test_executor_caches_domain_failure(storage: FileStorage):
    cache = MemoryCacheStorage()
    executor = JobExecutor(cache)
    digest = _DomainFailJob.make_input_digest(storage, payload="x")
    context = JobContext(input_digest=digest, storage=storage)

    first = _DomainFailJob("one")
    executor.execute(first, context)
    assert first.get_status() is JobStatus.FAILED
    assert first.get_output_digest() is not None

    second = _DomainFailJob("two")
    executor.execute(second, context)
    assert second.get_status() is JobStatus.FAILED
    assert second.get_failure_reason() == "Compilation failed"
    assert second.get_output_digest() == first.get_output_digest()


def test_executor_does_not_cache_bare_failure(storage: FileStorage):
    cache = MemoryCacheStorage()
    executor = JobExecutor(cache)
    digest = _BareFailJob.make_input_digest(storage, payload="x")
    context = JobContext(input_digest=digest, storage=storage)

    first = _BareFailJob("one")
    executor.execute(first, context)
    assert first.get_status() is JobStatus.FAILED
    assert first.get_output_digest() is None
    assert cache.get(digest, CachePolicy.Validator) is None
