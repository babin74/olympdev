"""Pipeline progress must survive a restart with nothing re-run.

If a pipeline keeps decision state outside its step table, these tests fail:
a restored pipeline would either redo finished work or lose its place.
"""

from pathlib import Path

import pytest

from olympdev.storage.filestorage import FileStorage
from olympdev.worker.executor import JobExecutor
from olympdev.worker.job import JobStatus
from olympdev.worker.pipeline import (
    AbstractPipeline,
    NextJobStatus,
    PipelineContext,
    PipelineState,
    PipelineStatus,
    ScheduledJob,
    run_pipeline,
)
from olympdev.worker.pipelines.problem_testing import ProblemTestingPipeline
from olympdev.worker.pipelines.problem_verification import (
    ProblemVerificationPipeline,
    SolutionVerificationSpec,
)
from olympdev.worker.verdict import Verdict

from test_problem_testing_pipeline import _configuration, _make_digest

pytestmark = pytest.mark.slow


def _context(storage, isolate_factory, input_digest) -> PipelineContext:
    return PipelineContext(
        input_digest=input_digest,
        storage=storage,
        isolate_factory=isolate_factory,
    )


def _roundtrip(pipeline: AbstractPipeline, storage: FileStorage, input_digest: str):
    """Save, push through storage, and rebuild - as a restarted worker would."""
    state_digest = pipeline.save_state(input_digest).upload(storage)
    state = PipelineState.require_from_storage(storage, state_digest)
    return AbstractPipeline.restore(state, storage)


def test_restored_pipeline_finishes_without_repeating_work(
    storage: FileStorage, isolate_factory, test_assets_path: Path, tmp_path: Path
):
    configuration = _configuration(tmp_path)
    input_digest = _make_digest(storage, test_assets_path, configuration, tmp_path)
    context = _context(storage, isolate_factory, input_digest)

    # Run only the compiles, then hand the pipeline over.
    original = ProblemTestingPipeline("restartable")
    executed_before = []
    while True:
        nxt = original.get_next_job(context)
        assert isinstance(nxt, ScheduledJob)
        nxt.job.execute(context.job_context(nxt.input_digest))
        executed_before.append(nxt.job.job_id)
        if not nxt.job.job_id.startswith("compile-"):
            break

    restored = _roundtrip(original, storage, input_digest)
    assert isinstance(restored, ProblemTestingPipeline)
    assert restored.pipeline_id == "restartable"

    executed_after = []
    while True:
        nxt = restored.get_next_job(context)
        if nxt is NextJobStatus.FINISHED:
            break
        assert isinstance(nxt, ScheduledJob), nxt
        nxt.job.execute(context.job_context(nxt.input_digest))
        executed_after.append(nxt.job.job_id)

    assert restored.get_status() is PipelineStatus.COMPLETED
    result = restored.get_result(storage)
    assert result is not None and result.verdict is Verdict.OK
    # Nothing already done was issued a second time.
    assert not set(executed_before) & set(executed_after)


def test_restore_carries_step_results_and_labels(
    storage: FileStorage, isolate_factory, test_assets_path: Path, tmp_path: Path
):
    configuration = _configuration(tmp_path)
    input_digest = _make_digest(
        storage, test_assets_path, configuration, tmp_path, problem_name="demo"
    )
    context = _context(storage, isolate_factory, input_digest)

    original = ProblemTestingPipeline("labelled")
    first = original.get_next_job(context)
    assert isinstance(first, ScheduledJob)
    first.job.execute(context.job_context(first.input_digest))

    restored = _roundtrip(original, storage, input_digest)
    step = restored.step("compile-solution")
    assert step is not None
    assert step.job.get_status() is JobStatus.COMPLETED
    assert step.job.get_output_digest() == first.job.get_output_digest()
    assert step.job.labels == first.job.labels
    assert step.input_digest == first.input_digest


def test_finished_pipeline_roundtrips_with_its_result(
    storage: FileStorage, isolate_factory, test_assets_path: Path, tmp_path: Path
):
    configuration = _configuration(tmp_path)
    input_digest = _make_digest(storage, test_assets_path, configuration, tmp_path)
    context = _context(storage, isolate_factory, input_digest)

    pipeline = ProblemTestingPipeline("done")
    run_pipeline(pipeline, context, executor=JobExecutor())

    restored = _roundtrip(pipeline, storage, input_digest)
    assert restored.get_status() is PipelineStatus.COMPLETED
    assert restored.get_output_digest() == pipeline.get_output_digest()
    # Children came back too, with their own results.
    assert len(restored.children()) == len(pipeline.children())
    assert restored.get_next_job(context) is NextJobStatus.FINISHED


def test_state_records_children_of_each_test(
    storage: FileStorage, isolate_factory, test_assets_path: Path, tmp_path: Path
):
    configuration = _configuration(tmp_path)
    input_digest = _make_digest(storage, test_assets_path, configuration, tmp_path)
    context = _context(storage, isolate_factory, input_digest)

    pipeline = ProblemTestingPipeline("children")
    run_pipeline(pipeline, context)

    state = pipeline.save_state(input_digest)
    assert set(state.children) == {"test-0"}
    child = state.children["test-0"]
    assert child.pipeline_type == "problem_test"
    assert child.status is PipelineStatus.COMPLETED
    assert {"run", "check"} <= set(child.steps)
    assert all(step.output_digest is not None for step in child.steps.values())


def _verification_digest(
    storage: FileStorage,
    test_assets_path: Path,
    configuration,
    problem_dir: Path,
) -> str:
    testing_digest = _make_digest(
        storage, test_assets_path, configuration, problem_dir
    )
    return ProblemVerificationPipeline.make_input_digest(
        storage,
        solutions=[
            SolutionVerificationSpec(
                name="sol", tag="main", testing_digest=testing_digest
            )
        ],
    )


def test_verification_pipeline_roundtrips_without_repeating_work(
    storage: FileStorage, isolate_factory, test_assets_path: Path, tmp_path: Path
):
    configuration = _configuration(tmp_path)
    input_digest = _verification_digest(
        storage, test_assets_path, configuration, tmp_path
    )
    context = _context(storage, isolate_factory, input_digest)

    original = ProblemVerificationPipeline("restartable-verify")
    executed_before = []
    while True:
        nxt = original.get_next_job(context)
        assert isinstance(nxt, ScheduledJob)
        nxt.job.execute(context.job_context(nxt.input_digest))
        executed_before.append(nxt.job.job_id)
        if not nxt.job.job_id.startswith("compile-"):
            break

    restored = _roundtrip(original, storage, input_digest)
    assert isinstance(restored, ProblemVerificationPipeline)
    assert restored.pipeline_id == "restartable-verify"
    child = restored.children()[0].pipeline
    assert isinstance(child, ProblemTestingPipeline)
    assert child.has_step("compile-solution")

    executed_after = []
    while True:
        nxt = restored.get_next_job(context)
        if nxt is NextJobStatus.FINISHED:
            break
        assert isinstance(nxt, ScheduledJob), nxt
        nxt.job.execute(context.job_context(nxt.input_digest))
        executed_after.append(nxt.job.job_id)

    assert restored.get_status() is PipelineStatus.COMPLETED
    result = restored.get_result(storage)
    assert result is not None and result.matched is True
    assert result.solutions[0].tag == "main"
    assert not set(executed_before) & set(executed_after)
