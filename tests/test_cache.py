from olympdev.worker.cache import (
    CachePolicy,
    CachedJobOutcome,
    MemoryCacheStorage,
    NullCacheStorage,
)
from olympdev.worker.job import JobStatus

_DIGEST = "a" * 64
_OUTCOME = CachedJobOutcome(
    output_digest="b" * 64,
    status=JobStatus.COMPLETED,
)


def test_memory_cache_put_get_roundtrip():
    cache = MemoryCacheStorage()
    cache.put(_DIGEST, CachePolicy.Validator, _OUTCOME)
    loaded = cache.get(_DIGEST, CachePolicy.Validator)
    assert loaded == _OUTCOME


def test_memory_cache_policies_do_not_overlap():
    cache = MemoryCacheStorage()
    cache.put(_DIGEST, CachePolicy.Validator, _OUTCOME)
    assert cache.get(_DIGEST, CachePolicy.Checker) is None
    assert cache.get(_DIGEST, CachePolicy.Validator) == _OUTCOME


def test_memory_cache_never_is_noop():
    cache = MemoryCacheStorage()
    cache.put(_DIGEST, CachePolicy.Never, _OUTCOME)
    assert cache.get(_DIGEST, CachePolicy.Never) is None


def test_null_cache_never_stores():
    cache = NullCacheStorage()
    cache.put(_DIGEST, CachePolicy.Validator, _OUTCOME)
    assert cache.get(_DIGEST, CachePolicy.Validator) is None
