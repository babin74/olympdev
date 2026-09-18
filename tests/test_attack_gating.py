"""The attack suite must not start by accident.

tests/attacks runs real fork bombs, memory hogs and host-file reads against
the host's isolate. If a sandbox is misconfigured, running them *is* the
attack - so no plain `pytest` invocation may pick them up. That is a safety
property, not a preference, so it gets a test of its own.
"""

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _pytest(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "pytest", "tests/attacks", "-q", *args],
        capture_output=True,
        text=True,
        cwd=REPO,
    )


def test_attacks_are_skipped_without_the_flag():
    result = _pytest("--collect-only")
    assert result.returncode == 0, result.stderr
    collected = result.stdout
    assert "test_payloads" in collected, "the attack tests should still be found"

    run = _pytest()
    assert run.returncode == 0, run.stderr
    # Everything skipped, nothing run: no payload was compiled or executed.
    assert "skipped" in run.stdout
    assert " passed" not in run.stdout, run.stdout


def test_the_full_suite_does_not_pull_in_attacks():
    """`pytest tests` is the documented full run; it must stay safe too."""
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests", "-q", "--collect-only", "-m", "attack"],
        capture_output=True,
        text=True,
        cwd=REPO,
    )
    assert result.returncode == 0, result.stderr
    # The marker exists and selects exactly the attack directory.
    for line in result.stdout.splitlines():
        if line.startswith("tests/"):
            assert line.startswith("tests/attacks/"), line
