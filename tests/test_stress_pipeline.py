"""The stress pipeline: finds a counterexample, exhausts its budget when
there isn't one, and survives a restart past 9 iterations without repeating
work - see worker/pipelines/stress.py's module docstring on why 9 matters
(PipelineState round-trips through sort_keys=True JSON; unpadded numeric
child names would silently reorder).
"""

import shutil
from pathlib import Path

import pytest

from olympdev.isolate import IsolateFactory
from olympdev.problem.build import build_stress_input
from olympdev.problem.package import ProblemPackage
from olympdev.storage.filestorage import FileStorage
from olympdev.worker.pipeline import (
    AbstractPipeline,
    NextJobStatus,
    PipelineContext,
    PipelineState,
    PipelineStatus,
    ScheduledJob,
    run_pipeline,
)
from olympdev.worker.pipelines.stress import StressPipeline

pytestmark = pytest.mark.slow


def _digest(
    storage: FileStorage,
    aplusb_dir: Path,
    *,
    candidate: str,
    against: str = "model",
    gen_args: str = "-c=1000",
    max_iterations: int = 1000,
) -> str:
    if shutil.which("g++") is None:
        pytest.skip("g++ is not available")
    package = ProblemPackage.load(aplusb_dir)
    return build_stress_input(
        storage,
        package,
        candidate=candidate,
        against=against,
        gen_args=gen_args,
        max_iterations=max_iterations,
    )


def test_stress_finds_a_counterexample_for_a_buggy_candidate(
    storage: FileStorage, isolate_factory: IsolateFactory, aplusb_dir: Path
):
    """sol-wa computes a-b instead of a+b - wrong on nearly every input."""
    digest = _digest(storage, aplusb_dir, candidate="sol-wa", against="model", max_iterations=50)
    pipeline = StressPipeline("stress-wa")
    run_pipeline(
        pipeline,
        PipelineContext(input_digest=digest, storage=storage, isolate_factory=isolate_factory),
    )
    assert pipeline.get_status() is PipelineStatus.COMPLETED
    result = pipeline.get_result(storage)
    assert result is not None
    assert result.found is True
    assert 1 <= result.iterations_run <= 50
    assert result.verdict is not None and result.verdict.value != "OK"
    assert result.input_digest is not None
    assert result.candidate_run_digest is not None
    assert result.reference_run_digest is not None
    assert result.check_digest is not None


def test_stress_exhausts_budget_for_a_correct_candidate(
    storage: FileStorage, isolate_factory: IsolateFactory, aplusb_dir: Path
):
    digest = _digest(storage, aplusb_dir, candidate="sol-ac", against="model", max_iterations=8)
    pipeline = StressPipeline("stress-ac")
    run_pipeline(
        pipeline,
        PipelineContext(input_digest=digest, storage=storage, isolate_factory=isolate_factory),
    )
    assert pipeline.get_status() is PipelineStatus.COMPLETED
    result = pipeline.get_result(storage)
    assert result is not None
    assert result.found is False
    assert result.iterations_run == 8
    assert result.verdict is None
    assert result.input_digest is None


def test_stress_pipeline_restores_past_ten_iterations_without_repeating_work(
    storage: FileStorage, isolate_factory: IsolateFactory, aplusb_dir: Path
):
    digest = _digest(storage, aplusb_dir, candidate="sol-ac", against="model", max_iterations=12)
    context = PipelineContext(
        input_digest=digest, storage=storage, isolate_factory=isolate_factory
    )
    original = StressPipeline("stress-roundtrip")
    executed_before: list[str] = []
    while len(original._children) < 10:
        nxt = original.get_next_job(context)
        assert isinstance(nxt, ScheduledJob), nxt
        nxt.job.execute(context.job_context(nxt.input_digest))
        executed_before.append(nxt.job.job_id)

    state_digest = original.save_state(digest).upload(storage)
    state = PipelineState.require_from_storage(storage, state_digest)
    restored = AbstractPipeline.restore(state, storage)
    assert isinstance(restored, StressPipeline)
    assert set(restored._children) == set(original._children)
    assert len(restored._children) >= 10

    executed_after: list[str] = []
    while True:
        nxt = restored.get_next_job(context)
        if nxt is NextJobStatus.FINISHED:
            break
        assert isinstance(nxt, ScheduledJob), nxt
        nxt.job.execute(context.job_context(nxt.input_digest))
        executed_after.append(nxt.job.job_id)

    assert restored.get_status() is PipelineStatus.COMPLETED
    result = restored.get_result(storage)
    assert result is not None
    assert result.found is False
    assert result.iterations_run == 12
    # Nothing already done (compiles, or the first 10 iterations) was issued
    # a second time.
    assert not set(executed_before) & set(executed_after)
