from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any
import hashlib
import json
import os
import re

from olympdev.settings import get_settings

_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")

#: Digest of zero bytes. Content addressing makes an empty blob recognisable
#: without fetching it, which is what lets a report skip offering an empty
#: stream's digest as if it were worth looking at.
EMPTY_BLOB_DIGEST = hashlib.sha256(b"").hexdigest()


class AbstractStorage(ABC):
    def get_bytes_digest(self, content: bytes) -> str:
        return hashlib.sha256(content).hexdigest()

    def get_file_digest(self, file_path: Path) -> str:
        fd = os.open(file_path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as f:
            return hashlib.file_digest(f, hashlib.sha256).hexdigest()

    def _dictionary_bytes(self, dictionary: dict[str, Any]) -> bytes:
        return json.dumps(dictionary, sort_keys=True, separators=(",", ":")).encode()

    def get_dictionary_digest(self, dictionary: dict[str, Any]) -> str:
        return self.get_bytes_digest(self._dictionary_bytes(dictionary))

    def validate_digest(self, digest: str) -> str:
        if not _SHA256_HEX.fullmatch(digest):
            raise ValueError(f"invalid digest: {digest!r}")
        return digest

    def upload_file(self, file_path: Path) -> str:
        return self._upload_file(file_path)

    def upload_bytes(self, content: bytes) -> str:
        return self._upload_bytes(content)

    def upload_dictionary(self, dictionary: dict[str, Any]) -> str:
        return self.upload_bytes(self._dictionary_bytes(dictionary))

    def download_file(self, digest: str, path: Path) -> bool:
        self.validate_digest(digest)
        return self._download_file(digest, path)

    def download_bytes(self, digest: str) -> bytes | None:
        self.validate_digest(digest)
        return self._download_bytes(digest)

    def download_dictionary(self, digest: str) -> dict[str, Any] | None:
        content = self.download_bytes(digest)
        if content is None:
            return None
        max_bytes = get_settings().limits.max_dictionary_bytes
        if len(content) > max_bytes:
            raise ValueError(
                f"dictionary blob too large: {len(content)} bytes "
                f"(max {max_bytes})"
            )
        data = json.loads(content)
        if not isinstance(data, dict):
            raise ValueError("dictionary blob is not a JSON object")
        return data

    def unlink(self, digest: str) -> bool:
        self.validate_digest(digest)
        return self._unlink(digest)

    def exists(self, digest: str) -> bool:
        self.validate_digest(digest)
        return self._exists(digest)

    def _exists(self, digest: str) -> bool:
        return self._download_bytes(digest) is not None

    @abstractmethod
    def _upload_file(self, file_path: Path) -> str: ...

    @abstractmethod
    def _upload_bytes(self, content: bytes) -> str: ...

    @abstractmethod
    def _download_file(self, digest: str, path: Path) -> bool: ...

    @abstractmethod
    def _download_bytes(self, digest: str) -> bytes | None: ...

    @abstractmethod
    def _unlink(self, digest: str) -> bool: ...
