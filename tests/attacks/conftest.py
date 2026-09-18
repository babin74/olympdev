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


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Mark the attack tests, and keep them from starting by accident.

    These run real fork bombs and memory hogs against the host's isolate. A
    misconfigured sandbox turns them into an attack on the machine running
    them, so `pytest tests` must not pick them up - only an explicit
    `--run-attacks` does.

    This hook is handed the whole session's items, not just ours, so it marks
    only the attack tests. Marking everything made `-m "not slow"` empty.
    """
    requested = config.getoption("--run-attacks")
    skip = pytest.mark.skip(reason="adversarial payloads; pass --run-attacks to run")
    for item in items:
        if _ATTACKS_DIR not in Path(item.path).parents:
            continue
        item.add_marker(pytest.mark.slow)
        item.add_marker(pytest.mark.attack)
        if not requested:
            item.add_marker(skip)


@pytest.fixture(scope="session")
def isolate_enforced() -> None:
    probe_isolate_enforces_limits(IsolateFactory(1))


@pytest.fixture(autouse=True)
def _host_guard(isolate_enforced: None) -> None:
    assert_host_markers_absent()
    yield
    assert_host_markers_absent()
    assert_host_rss_bounded()
