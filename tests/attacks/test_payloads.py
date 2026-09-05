"""Adversarial submissions must be contained by the sandbox, not by luck.

Each payload goes through ProblemTestingPipeline exactly as a participant's
submission would. A payload never runs on the host: if isolate failed to
contain it, the assertions below fail rather than the machine falling over.

The harness aborts the whole suite up front (probe_isolate_enforces_limits) if
isolate would not enforce wall time or memory on this host.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from harness import (
    CONTAINED_VERDICTS,
    COMPILE_PIPELINE_CAP_SEC,
    assert_compile_limits,
    assert_contained,
    assert_host_markers_absent,
    assert_pipeline_survived,
    assert_run_limits,
    payload_bytes,
    run_aplusb_submission,
)

from olympdev.storage.filestorage import FileStorage
from olympdev.worker.verdict import Verdict

#: Payloads the compiler must reject - either as bad code, or by hitting the
#: compile job's own time/memory limits rather than exhausting the host.
COMPILE_ERROR_PAYLOADS = (
    "compile_syntax.cpp",
    "compile_macro_bomb.cpp",
    "compile_recursive_include.cpp",
    "compile_constexpr_fib.cpp",
    "compile_include_dev_zero.cpp",
)

#: Payloads that make the compiler do a lot of work but still succeed
#: (compile_template_layout builds a 2**24-byte struct). The point is that the
#: compile stayed inside its limits; the verdict is then an ordinary one.
COMPILE_STRESS_PAYLOADS = ("compile_template_layout.cpp",)

#: Payloads aimed at the runner.
RUN_PAYLOADS = (
    ("run_tl_busy.cpp", Verdict.TL),
    ("run_tl_sleep.cpp", Verdict.TL),
    ("run_ml_touch.cpp", {Verdict.ML, Verdict.RE}),
)


@pytest.mark.parametrize("payload", COMPILE_ERROR_PAYLOADS)
def test_compile_payload_is_rejected(
    storage: FileStorage, isolate_factory, aplusb_dir: Path, payload: str
):
    outcome = run_aplusb_submission(
        storage,
        isolate_factory,
        aplusb_dir,
        payload_bytes(payload),
        pipeline_cap_sec=COMPILE_PIPELINE_CAP_SEC,
    )
    assert_contained(outcome, CONTAINED_VERDICTS)
    assert outcome.result is not None
    assert outcome.result.verdict is Verdict.CE, (
        f"{payload}: expected a compile error, got {outcome.result.verdict}"
    )
    assert_compile_limits(outcome)


@pytest.mark.parametrize("payload", COMPILE_STRESS_PAYLOADS)
def test_compile_stress_payload_stays_within_compile_limits(
    storage: FileStorage, isolate_factory, aplusb_dir: Path, payload: str
):
    outcome = run_aplusb_submission(
        storage,
        isolate_factory,
        aplusb_dir,
        payload_bytes(payload),
        pipeline_cap_sec=COMPILE_PIPELINE_CAP_SEC,
    )
    assert_contained(outcome, CONTAINED_VERDICTS)
    assert_compile_limits(outcome)


@pytest.mark.parametrize("payload,expected", RUN_PAYLOADS)
def test_run_payload_is_contained(
    storage: FileStorage, isolate_factory, aplusb_dir: Path, payload: str, expected
):
    outcome = run_aplusb_submission(
        storage, isolate_factory, aplusb_dir, payload_bytes(payload)
    )
    assert_contained(outcome, CONTAINED_VERDICTS)
    assert outcome.result is not None
    allowed = expected if isinstance(expected, set) else {expected}
    assert outcome.result.verdict in allowed, (
        f"{payload}: expected one of {sorted(v.value for v in allowed)}, "
        f"got {outcome.result.verdict}"
    )
    assert_run_limits(outcome)


def test_including_a_host_file_does_not_leak_it(
    storage: FileStorage, isolate_factory, aplusb_dir: Path
):
    """A payload that #includes a host path must not end up with its contents."""
    outcome = run_aplusb_submission(
        storage,
        isolate_factory,
        aplusb_dir,
        payload_bytes("compile_include_host.cpp"),
        pipeline_cap_sec=COMPILE_PIPELINE_CAP_SEC,
    )
    assert_pipeline_survived(outcome)
    assert outcome.result is not None
    assert outcome.result.verdict is not Verdict.OK
    for stdout in outcome.run_stdouts():
        assert b"root:" not in stdout, "payload leaked /etc/passwd through stdout"


def test_payloads_never_write_outside_the_sandbox(
    storage: FileStorage, isolate_factory, aplusb_dir: Path
):
    """The autouse guard checks this too; assert it explicitly for one run."""
    run_aplusb_submission(
        storage, isolate_factory, aplusb_dir, payload_bytes("run_tl_busy.cpp")
    )
    assert_host_markers_absent()
