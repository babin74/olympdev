from olympdev.worker.cache.storage import (
    AbstractCacheStorage,
    CachedJobOutcome,
    NullCacheStorage,
)
from olympdev.worker.job import AbstractJob, JobContext


def _failure_reason_str(reason: object) -> str | None:
    if reason is None:
        return None
    return str(reason)


class JobExecutor:
    """Runs a job, completing it from cache when the policy allows."""

    def __init__(self, cache: AbstractCacheStorage | None = None):
        self.cache = cache if cache is not None else NullCacheStorage()

    def execute(self, job: AbstractJob, context: JobContext) -> None:
        policy = job.get_cache_policy()
        outcome = self.cache.get(context.input_digest, policy)
        if outcome is not None:
            job.complete_from_cache(outcome)
            return
        job.execute(context)
        output_digest = job.get_output_digest()
        if output_digest is None:
            return
        self.cache.put(
            context.input_digest,
            policy,
            CachedJobOutcome(
                output_digest=output_digest,
                status=job.get_status(),
                failure_reason=_failure_reason_str(job.get_failure_reason()),
            ),
        )
