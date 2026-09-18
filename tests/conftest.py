from pathlib import Path

import pytest

from olympdev.isolate import IsolateFactory
from olympdev.settings import Settings, set_settings
from olympdev.storage.filestorage import FileStorage

RUN_ATTACKS_OPTION = "--run-attacks"


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        RUN_ATTACKS_OPTION,
        action="store_true",
        default=False,
        help=(
            "also run tests/attacks: adversarial payloads (fork bombs, memory "
            "hogs, host-file reads) executed for real. Opt-in on purpose - "
            "they must never start by accident."
        ),
    )


@pytest.fixture(autouse=True)
def default_settings():
    """Pin built-in settings so a host's olympdev.toml cannot skew tests."""
    set_settings(Settings())
    yield
    set_settings(None)


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
