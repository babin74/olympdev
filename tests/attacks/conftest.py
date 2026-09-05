"""Fixtures for adversarial aplusb submissions.

Autouse guards refuse to run if isolate is not actually killing programs.
Payloads are bounded so a missed limit should not take down the WSL VM.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from olympdev.isolate import IsolateFactory

_ATTACKS_DIR = Path(__file__).resolve().parent
if str(_ATTACKS_DIR) not in sys.path:
    sys.path.insert(0, str(_ATTACKS_DIR))

from harness import (
    assert_host_markers_absent,
    assert_host_rss_bounded,
    probe_isolate_enforces_limits,
)


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        item.add_marker(pytest.mark.slow)


@pytest.fixture(scope="session")
def isolate_enforced() -> None:
    probe_isolate_enforces_limits(IsolateFactory(1))


@pytest.fixture(autouse=True)
def _host_guard(isolate_enforced: None) -> None:
    assert_host_markers_absent()
    yield
    assert_host_markers_absent()
    assert_host_rss_bounded()
