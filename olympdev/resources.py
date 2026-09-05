from __future__ import annotations

from contextlib import contextmanager
from importlib.resources import as_file, files
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import Iterator

import olympdev


def asset_path(name: str) -> Traversable:
    """Return a traversable path to a bundled package asset."""
    return files(olympdev) / "assets" / name


@contextmanager
def asset_file(name: str) -> Iterator[Path]:
    """Yield a filesystem path to a bundled package asset."""
    with as_file(asset_path(name)) as path:
        yield path


def read_asset_bytes(name: str) -> bytes:
    return asset_path(name).read_bytes()


def read_asset_text(name: str, *, encoding: str = "utf-8") -> str:
    return asset_path(name).read_text(encoding=encoding)
