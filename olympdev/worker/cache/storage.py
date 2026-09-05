from abc import ABC, abstractmethod

from olympdev.schema import FrozenModel
from olympdev.worker.cache.policy import CachePolicy
from olympdev.worker.job import JobStatus


class CachedJobOutcome(FrozenModel):
    output_digest: str | None
    status: JobStatus
    failure_reason: str | None = None


class AbstractCacheStorage(ABC):
    """Maps ``(input_digest, cache_policy)`` to a finished job outcome.

    Whether a policy is stored is decided here, not by the executor.
    """

    def should_cache(self, policy: CachePolicy) -> bool:
        return policy is not CachePolicy.Never

    def get(self, input_digest: str, policy: CachePolicy) -> CachedJobOutcome | None:
        if not self.should_cache(policy):
            return None
        return self._get(input_digest, policy)

    def put(
        self,
        input_digest: str,
        policy: CachePolicy,
        outcome: CachedJobOutcome,
    ) -> None:
        if not self.should_cache(policy):
            return
        self._put(input_digest, policy, outcome)

    @abstractmethod
    def _get(
        self, input_digest: str, policy: CachePolicy
    ) -> CachedJobOutcome | None: ...

    @abstractmethod
    def _put(
        self,
        input_digest: str,
        policy: CachePolicy,
        outcome: CachedJobOutcome,
    ) -> None: ...


class NullCacheStorage(AbstractCacheStorage):
    def should_cache(self, policy: CachePolicy) -> bool:
        return False

    def _get(
        self, input_digest: str, policy: CachePolicy
    ) -> CachedJobOutcome | None:
        return None

    def _put(
        self,
        input_digest: str,
        policy: CachePolicy,
        outcome: CachedJobOutcome,
    ) -> None:
        return


class MemoryCacheStorage(AbstractCacheStorage):
    def __init__(self) -> None:
        self._entries: dict[tuple[CachePolicy, str], CachedJobOutcome] = {}

    def _get(
        self, input_digest: str, policy: CachePolicy
    ) -> CachedJobOutcome | None:
        return self._entries.get((policy, input_digest))

    def _put(
        self,
        input_digest: str,
        policy: CachePolicy,
        outcome: CachedJobOutcome,
    ) -> None:
        self._entries[(policy, input_digest)] = outcome
