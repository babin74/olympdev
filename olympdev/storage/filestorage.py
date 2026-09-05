from olympdev.storage.abstract import AbstractStorage
from pathlib import Path
import hashlib
import os
import shutil


class FileStorage(AbstractStorage):
    def __init__(self, root_path: Path):
        self.root_path = Path(root_path)
        self.root_path.mkdir(parents=True, exist_ok=True)

    def _get_file_path(self, digest: str) -> Path:
        return self.root_path / digest

    def _exists(self, digest: str) -> bool:
        return self._get_file_path(digest).is_file()

    def _upload_file(self, file_path: Path) -> str:
        fd = os.open(file_path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as src:
            digest = hashlib.file_digest(src, hashlib.sha256).hexdigest()
            target_path = self._get_file_path(digest)
            if not target_path.exists():
                src.seek(0)
                with open(target_path, "wb") as dst:
                    shutil.copyfileobj(src, dst)
        return digest

    def _upload_bytes(self, content: bytes) -> str:
        digest = self.get_bytes_digest(content)
        target_path = self._get_file_path(digest)
        if not target_path.exists():
            target_path.write_bytes(content)
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
