from olympdev.worker.cache.policy import CachePolicy

__all__ = [
    "AbstractCacheStorage",
    "CachePolicy",
    "CachedJobOutcome",
    "MemoryCacheStorage",
    "NullCacheStorage",
]


def __getattr__(name: str):
    if name in {
        "AbstractCacheStorage",
        "CachedJobOutcome",
        "MemoryCacheStorage",
        "NullCacheStorage",
    }:
        from olympdev.worker.cache import storage as _storage

        return getattr(_storage, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
