from pathlib import Path

import pytest

from olympdev.isolate import IsolateFactory
from olympdev.storage.filestorage import FileStorage


@pytest.fixture(scope="session")
def test_assets_path() -> Path:
    return Path(__file__).parent / "assets"


@pytest.fixture(scope="session")
def aplusb_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "example-contest" / "aplusb"


@pytest.fixture
def storage(tmp_path: Path) -> FileStorage:
    return FileStorage(tmp_path / "storage")


@pytest.fixture
def isolate_factory() -> IsolateFactory:
    return IsolateFactory(1)
