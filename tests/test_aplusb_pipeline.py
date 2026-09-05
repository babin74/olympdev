import shutil
from pathlib import Path

import pytest

from olympdev.isolate import IsolateFactory
from olympdev.problem.build import build_problem_testing_input
from olympdev.problem.package import ProblemPackage
from olympdev.storage.filestorage import FileStorage
from olympdev.worker.job import JobStatus
from olympdev.worker.jobs.simple_run import SimpleRunJob
from olympdev.worker.jobs.tool_run import ToolRunJob
from olympdev.worker.pipeline import (
    NextJobStatus,
    PipelineContext,
    PipelineStatus,
    ScheduledJob,
    run_pipeline,
)
from olympdev.worker.pipelines.problem_testing import ProblemTestingPipeline
from olympdev.worker.roles import ProgramRole
from olympdev.worker.verdict import Verdict

pytestmark = pytest.mark.slow

_STATIC_SAMPLES = ("tests/sample-01.txt", "tests/sample-02.txt")
_STATIC_TESTS = ("tests/manual-01.txt",)
_GENERATED_TESTS = (
    ("gen -c=1e3", "random-01.txt"),
    ("gen -c=1e6", "random-02.txt"),
    ("gen -c=1e9", "random-03.txt"),
)
_TEST_NAMES = (*_STATIC_SAMPLES, *_STATIC_TESTS, *(name for _, name in _GENERATED_TESTS))
_COMPILE_IDS = (
    "compile-solution",
    "compile-checker",
    "compile-model",
    "compile-generator",
    "compile-validator",
)


def _aplusb_digest(
    storage: FileStorage,
    aplusb_dir: Path,
    *,
    source: Path | str | bytes | None = None,
) -> str:
    if shutil.which("g++") is None:
        pytest.skip("g++ is not available")
    return build_problem_testing_input(
        storage, ProblemPackage.load(aplusb_dir), solution=source
    )


def _run(storage: FileStorage, isolate_factory: IsolateFactory, input_digest: str):
    pipeline = ProblemTestingPipeline("aplusb")
    run_pipeline(
        pipeline,
        PipelineContext(
            input_digest=input_digest,
            storage=storage,
            isolate_factory=isolate_factory,
        ),
    )
    return pipeline


def _context(
    storage: FileStorage,
    isolate_factory: IsolateFactory,
    input_digest: str,
) -> PipelineContext:
    return PipelineContext(
        input_digest=input_digest,
        storage=storage,
        isolate_factory=isolate_factory,
    )


def _execute_compiles(pipeline: ProblemTestingPipeline, context: PipelineContext) -> None:
    for job_id in _COMPILE_IDS:
        job = pipeline.get_next_job(context)
        assert isinstance(job, ScheduledJob)
        assert job.job.job_id == job_id
        job.job.execute(context.job_context(job.input_digest))
        assert job.job.get_status() is JobStatus.COMPLETED


def test_aplusb_main_solution_ok_without_output_files(
    storage: FileStorage,
    isolate_factory: IsolateFactory,
    aplusb_dir: Path,
):
    input_digest = _aplusb_digest(
        storage, aplusb_dir, source="solutions/sol-ac.cpp"
    )
    arguments = storage.download_dictionary(input_digest)
    assert set(arguments["compiles"]) == {
        "solution", "checker", "model", "generator", "validator",
    }
    assert arguments["binary_names"]["model"] == "sol-ac"
    assert arguments["binary_names"]["generator"] == "gen"
    assert arguments["binary_names"]["validator"] == "valid"
    assert all(test["output_digest"] is None for test in arguments["tests"])
    assert [test["name"] for test in arguments["tests"]] == list(_TEST_NAMES)
    assert arguments["tests"][2]["input_digest"] is not None
    assert arguments["tests"][2]["gen_argv"] is None
    assert arguments["tests"][3]["input_digest"] is None
    assert arguments["tests"][3]["gen_argv"] == ["-c=1e3"]
    assert arguments["tests"][4]["gen_argv"] == ["-c=1e6"]
    assert arguments["tests"][5]["gen_argv"] == ["-c=1e9"]

    pipeline = _run(storage, isolate_factory, input_digest)

    assert pipeline.get_status() is PipelineStatus.COMPLETED
    result = pipeline.get_result(storage)
    assert result is not None
    assert result.verdict is Verdict.OK
    assert set(result.compile_digests) == {
        ProgramRole.SOLUTION,
        ProgramRole.CHECKER,
        ProgramRole.MODEL,
        ProgramRole.GENERATOR,
        ProgramRole.VALIDATOR,
    }
    assert [test.name for test in result.tests] == list(_TEST_NAMES)
    assert [test.verdict for test in result.tests] == [Verdict.OK] * len(_TEST_NAMES)
    assert all(test.check_digest is not None for test in result.tests)


def test_aplusb_wa_solution_is_wrong_against_generated_answers(
    storage: FileStorage,
    isolate_factory: IsolateFactory,
    aplusb_dir: Path,
):
    input_digest = _aplusb_digest(
        storage, aplusb_dir, source="solutions/sol-wa.cpp"
    )
    pipeline = _run(storage, isolate_factory, input_digest)

    result = pipeline.get_result(storage)
    assert pipeline.get_status() is PipelineStatus.COMPLETED
    assert result is not None
    assert result.verdict is Verdict.WA
    assert result.tests[0].verdict is Verdict.WA
    assert result.tests[0].check_digest is not None


def test_aplusb_validates_static_input_before_run_and_model(
    storage: FileStorage,
    isolate_factory: IsolateFactory,
    aplusb_dir: Path,
):
    input_digest = _aplusb_digest(
        storage, aplusb_dir, source="solutions/sol-ac.cpp"
    )
    pipeline = ProblemTestingPipeline("aplusb")
    context = _context(storage, isolate_factory, input_digest)
    args = pipeline._parse_arguments(context)
    assert args.binary_names[ProgramRole.VALIDATOR] == "valid"
    assert args.labels(ProgramRole.VALIDATOR, "tests/sample-01.txt") == {
        "Problem": "aplusb",
        "Test": "sample-01.txt",
        "Binary": "valid",
    }

    _execute_compiles(pipeline, context)

    valid0 = pipeline.get_next_job(context)
    assert isinstance(valid0, ScheduledJob)
    assert isinstance(valid0.job, ToolRunJob)
    assert valid0.job.job_id == "valid-0"
    valid_arguments = storage.download_dictionary(valid0.input_digest)
    assert valid_arguments["stdin_digest"] is not None
    assert valid_arguments["command"] == []
    valid0.job.execute(context.job_context(valid0.input_digest))

    run0 = pipeline.get_next_job(context)
    assert isinstance(run0, ScheduledJob)
    assert isinstance(run0.job, SimpleRunJob)
    assert run0.job.job_id == "run-0"
    run0.job.execute(context.job_context(run0.input_digest))

    model0 = pipeline.get_next_job(context)
    assert isinstance(model0, ScheduledJob)
    assert isinstance(model0.job, SimpleRunJob)
    assert model0.job.job_id == "model-0"
    assert model0.job.labels == {
        "Problem": "aplusb",
        "Test": "sample-01.txt",
        "Binary": "sol-ac",
    }
    model0.job.execute(context.job_context(model0.input_digest))

    check0 = pipeline.get_next_job(context)
    assert isinstance(check0, ScheduledJob)
    assert isinstance(check0.job, ToolRunJob)
    assert check0.job.job_id == "check-0"
    check_arguments = storage.download_dictionary(check0.input_digest)
    assert "answer.txt" in check_arguments["files_paths"]
    assert check_arguments["command"] == ["input.txt", "output.txt", "answer.txt"]


def test_aplusb_generated_test_runs_gen_then_valid_then_run(
    storage: FileStorage,
    isolate_factory: IsolateFactory,
    aplusb_dir: Path,
):
    input_digest = _aplusb_digest(
        storage, aplusb_dir, source="solutions/sol-ac.cpp"
    )
    pipeline = ProblemTestingPipeline("aplusb")
    context = _context(storage, isolate_factory, input_digest)
    _execute_compiles(pipeline, context)

    first_jobs = {}
    for expected_id in ("valid-0", "valid-1", "valid-2", "gen-3", "gen-4", "gen-5"):
        job = pipeline.get_next_job(context)
        assert isinstance(job, ScheduledJob)
        assert job.job.job_id == expected_id
        first_jobs[expected_id] = job
    assert pipeline.get_next_job(context) is NextJobStatus.WAITING

    gen3 = first_jobs["gen-3"]
    assert isinstance(gen3.job, ToolRunJob)
    assert gen3.job.labels == {
        "Problem": "aplusb",
        "Test": "random-01.txt",
        "Binary": "gen",
    }
    gen_arguments = storage.download_dictionary(gen3.input_digest)
    assert gen_arguments["command"] == ["-c=1e3"]
    gen3.job.execute(context.job_context(gen3.input_digest))
    assert gen3.job.get_status() is JobStatus.COMPLETED

    valid3 = pipeline.get_next_job(context)
    assert isinstance(valid3, ScheduledJob)
    assert isinstance(valid3.job, ToolRunJob)
    assert valid3.job.job_id == "valid-3"
    valid3.job.execute(context.job_context(valid3.input_digest))
    assert valid3.job.get_status() is JobStatus.COMPLETED

    run3 = pipeline.get_next_job(context)
    assert isinstance(run3, ScheduledJob)
    assert isinstance(run3.job, SimpleRunJob)
    assert run3.job.job_id == "run-3"


def test_aplusb_starts_next_test_while_model_generates_answer(
    storage: FileStorage,
    isolate_factory: IsolateFactory,
    aplusb_dir: Path,
):
    input_digest = _aplusb_digest(
        storage, aplusb_dir, source="solutions/sol-ac.cpp"
    )
    pipeline = ProblemTestingPipeline("aplusb")
    context = _context(storage, isolate_factory, input_digest)
    _execute_compiles(pipeline, context)

    valid0 = pipeline.get_next_job(context)
    assert isinstance(valid0, ScheduledJob)
    assert valid0.job.job_id == "valid-0"
    valid0.job.execute(context.job_context(valid0.input_digest))

    run0 = pipeline.get_next_job(context)
    assert isinstance(run0, ScheduledJob)
    assert run0.job.job_id == "run-0"
    run0.job.execute(context.job_context(run0.input_digest))

    model0 = pipeline.get_next_job(context)
    assert isinstance(model0, ScheduledJob)
    assert model0.job.job_id == "model-0"

    valid1 = pipeline.get_next_job(context)
    assert isinstance(valid1, ScheduledJob)
    assert valid1.job.job_id == "valid-1"

    valid2 = pipeline.get_next_job(context)
    assert isinstance(valid2, ScheduledJob)
    assert valid2.job.job_id == "valid-2"

    gen3 = pipeline.get_next_job(context)
    assert isinstance(gen3, ScheduledJob)
    assert gen3.job.job_id == "gen-3"

    gen4 = pipeline.get_next_job(context)
    assert isinstance(gen4, ScheduledJob)
    assert gen4.job.job_id == "gen-4"

    gen5 = pipeline.get_next_job(context)
    assert isinstance(gen5, ScheduledJob)
    assert gen5.job.job_id == "gen-5"
    assert pipeline.get_next_job(context) is NextJobStatus.WAITING
