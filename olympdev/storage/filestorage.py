from olympdev.storage.abstract import AbstractStorage
from pathlib import Path
import hashlib
import os
import shutil
import tempfile

# Blobs are sharded by the first bytes of their digest so a contest's worth of
# files does not land in one directory.
_SHARD_DEPTH = 2
_SHARD_WIDTH = 2


class FileStorage(AbstractStorage):
    """Content-addressed blobs on local disk.

    Writes are atomic: content lands in a temporary file inside the storage
    root and is then renamed into place, so a concurrent reader either sees a
    complete blob or none at all. This matters as soon as more than one worker
    process shares a storage root.
    """

    def __init__(self, root_path: Path):
        self.root_path = Path(root_path)
        self.root_path.mkdir(parents=True, exist_ok=True)

    def _get_file_path(self, digest: str) -> Path:
        shards = [
            digest[i * _SHARD_WIDTH:(i + 1) * _SHARD_WIDTH]
            for i in range(_SHARD_DEPTH)
        ]
        return self.root_path.joinpath(*shards, digest)

    def _commit(self, temp_path: Path, digest: str) -> None:
        """Move a fully written temporary file into its digest location."""
        target_path = self._get_file_path(digest)
        if target_path.exists():
            temp_path.unlink(missing_ok=True)
            return
        target_path.parent.mkdir(parents=True, exist_ok=True)
        # os.replace is atomic within a filesystem; both paths are under root.
        os.replace(temp_path, target_path)

    def _temp_file(self):
        return tempfile.NamedTemporaryFile(
            dir=self.root_path, prefix=".tmp-", delete=False
        )

    def _exists(self, digest: str) -> bool:
        return self._get_file_path(digest).is_file()

    def _upload_file(self, file_path: Path) -> str:
        fd = os.open(file_path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as src:
            digest = hashlib.file_digest(src, hashlib.sha256).hexdigest()
            if self._exists(digest):
                return digest
            src.seek(0)
            temp = self._temp_file()
            try:
                with temp:
                    shutil.copyfileobj(src, temp)
                self._commit(Path(temp.name), digest)
            except BaseException:
                Path(temp.name).unlink(missing_ok=True)
                raise
        return digest

    def _upload_bytes(self, content: bytes) -> str:
        digest = self.get_bytes_digest(content)
        if self._exists(digest):
            return digest
        temp = self._temp_file()
        try:
            with temp:
                temp.write(content)
            self._commit(Path(temp.name), digest)
        except BaseException:
            Path(temp.name).unlink(missing_ok=True)
            raise
        return digest

    def _download_file(self, digest: str, path: Path) -> bool:
        source_path = self._get_file_path(digest)
        if not source_path.is_file() or source_path.is_symlink():
            return False
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_path, destination)
        return True

    def _download_bytes(self, digest: str) -> bytes | None:
        try:
            path = self._get_file_path(digest)
            if path.is_symlink():
                return None
            return path.read_bytes()
        except FileNotFoundError:
            return None

    def _unlink(self, digest: str) -> bool:
        try:
            self._get_file_path(digest).unlink()
            return True
        except FileNotFoundError:
            return False

    def get_root_path(self) -> Path:
        return self.root_path
