"""A local cache in front of a shared blob store.

This is the seam that lets a worker hold nothing but digests: when a job asks
for a blob the worker does not have, it is fetched from the shared backend and
kept locally, so repeated tests of the same problem pay the transfer once.

``remote`` is any :class:`AbstractStorage`. Today that means another
``FileStorage`` on a shared mount; an HTTP or S3 backend slots in here without
touching jobs or pipelines, since content addressing makes every blob
immutable and safe to cache forever.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from olympdev.storage.abstract import AbstractStorage


class CachingStorage(AbstractStorage):
    def __init__(self, local: AbstractStorage, remote: AbstractStorage):
        self.local = local
        self.remote = remote

    # --- reads: local first, then pull through and keep a copy ---

    def _exists(self, digest: str) -> bool:
        return self.local.exists(digest) or self.remote.exists(digest)

    def _pull(self, digest: str) -> bytes | None:
        content = self.remote.download_bytes(digest)
        if content is None:
            return None
        self.local.upload_bytes(content)
        return content

    def _download_bytes(self, digest: str) -> bytes | None:
        content = self.local.download_bytes(digest)
        if content is not None:
            return content
        return self._pull(digest)

    def _download_file(self, digest: str, path: Path) -> bool:
        if self.local.download_file(digest, path):
            return True
        if self._pull(digest) is None:
            return False
        return self.local.download_file(digest, path)

    # --- writes: local and remote both get the blob ---

    def _upload_bytes(self, content: bytes) -> str:
        digest = self.local.upload_bytes(content)
        remote_digest = self.remote.upload_bytes(content)
        assert digest == remote_digest
        return digest

    def _upload_file(self, file_path: Path) -> str:
        digest = self.local.upload_file(file_path)
        remote_digest = self.remote.upload_file(file_path)
        assert digest == remote_digest
        return digest

    def _unlink(self, digest: str) -> bool:
        """Remove from both tiers; a blob left remotely would just come back."""
        local_removed = self.local.unlink(digest)
        remote_removed = self.remote.unlink(digest)
        return local_removed or remote_removed

    def evict(self, digest: str) -> bool:
        """Drop the local copy only, keeping the blob available remotely."""
        return self.local.unlink(digest)


def open_storage(settings: Any | None = None) -> AbstractStorage:
    """Build the storage this host is configured for.

    With ``storage.remote_root`` set, ``storage.root`` becomes a pull-through
    cache in front of it; otherwise the local root is the whole store.
    """
    from olympdev.settings import get_settings
    from olympdev.storage.filestorage import FileStorage

    if settings is None:
        settings = get_settings()
    local = FileStorage(settings.storage.root)
    if settings.storage.remote_root is None:
        return local
    return CachingStorage(local, FileStorage(settings.storage.remote_root))
