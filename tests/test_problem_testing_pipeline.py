import shutil
from pathlib import Path

import pytest
from pydantic import ValidationError

from olympdev.isolate import IsolateFactory, IsolateMeta
from olympdev.problem.types import ProblemConfiguration, ProblemTest, parse_gen_argv
from olympdev.storage.filestorage import FileStorage
from olympdev.worker.jobs.simple_run import SimpleRunJob
from olympdev.worker.jobs.tool_run import ToolRunJob
from olympdev.worker.pipeline import (
    NextJobStatus,
    PipelineContext,
    PipelineStatus,
    ScheduledJob,
    run_pipeline,
)
from olympdev.worker.pipelines.problem_test import ProblemTestingCase, RunLimits
from olympdev.worker.roles import ProgramRole
from olympdev.worker.pipelines.problem_testing import (
    CompileSpec,
    ProblemTestingArguments,
    ProblemTestingPipeline,
    ProblemTestingResult,
    ProblemTestingTestResult,
)
from olympdev.worker.verdict import Verdict


def _configuration(
    problem_dir: Path,
    *,
    expected: bytes = b"Hello, output world!\n",
    aspace_limit: int | None = None,
    file_size_limit: int | None = 2048,
    extra_tests: list[ProblemTest] | None = None,
) -> ProblemConfiguration:
    sample = problem_dir / "sample.in"
    sample_out = problem_dir / "sample.out"
    sample.write_bytes(b"")
    sample_out.write_bytes(expected)
    tests = extra_tests or []
    return ProblemConfiguration(
        time_limit=1000,
        memory_limit=64,
        aspace_limit=aspace_limit,
        file_size_limit=file_size_limit,
        input_file_name="input.txt",
        output_file_name="output.txt",
        samples=[ProblemTest(input_file="sample.in", output_file="sample.out")],
        tests=tests,
        solutions=[],
        sources=[],
    )


def _make_digest(
    storage: FileStorage,
    test_assets_path: Path,
    configuration: ProblemConfiguration,
    problem_dir: Path,
    *,
    source: Path | bytes | None = None,
    command: list[str | int] | None = None,
    problem_name: str | None = None,
) -> str:
    gxx = shutil.which("g++")
    if gxx is None:
        pytest.skip("g++ is not available")
    if source is None:
        source = test_assets_path / "hello.cpp"
    if command is None:
        command = [gxx, "-O2", "-o", "solution", 0]
    return ProblemTestingPipeline.make_input_digest(
        storage,
        programs={
            ProgramRole.SOLUTION: CompileSpec(
                sources=[(source, "main.cpp")], command=command
            ),
            ProgramRole.CHECKER: CompileSpec(
                sources=[(test_assets_path / "exact_checker.cpp", "checker.cpp")],
                command=[gxx, "-O2", "-o", "checker", 0],
            ),
        },
        configuration=configuration,
        problem_dir=problem_dir,
        problem_name=problem_name,
    )


def test_problem_testing_result_roundtrip(storage: FileStorage):
    result = ProblemTestingResult(
        verdict=Verdict.WA,
        compile_digests={
            ProgramRole.SOLUTION: storage.upload_bytes(b"compile"),
            ProgramRole.CHECKER: storage.upload_bytes(b"checker"),
        },
        tests=[
            ProblemTestingTestResult(
                name="sample.in",
                verdict=Verdict.OK,
                run_digest=storage.upload_bytes(b"run"),
                check_digest=storage.upload_bytes(b"check"),
            ),
            ProblemTestingTestResult(
                name="tests/2.in",
                verdict=Verdict.WA,
                run_digest=None,
                check_digest=None,
            ),
        ],
    )

    digest = result.upload(storage)
    loaded = ProblemTestingResult.from_storage(storage, digest)

    assert loaded == result
    assert loaded.to_dict() == result.to_dict()


def test_problem_testing_result_from_dict():
    data = {
        "verdict": "CE",
        "compile_digests": {"solution": "a" * 64, "checker": "b" * 64},
        "tests": [],
    }

    result = ProblemTestingResult.from_dict(data)

    assert result.verdict is Verdict.CE
    assert result.compile_digests[ProgramRole.SOLUTION] == "a" * 64
    assert result.compile_digests[ProgramRole.CHECKER] == "b" * 64
    assert result.tests == []


def test_make_input_digest_stores_aspace_limit(
    storage: FileStorage,
    test_assets_path: Path,
    tmp_path: Path,
):
    configuration = _configuration(tmp_path, aspace_limit=4096)
    input_digest = _make_digest(storage, test_assets_path, configuration, tmp_path)

    arguments = storage.download_dictionary(input_digest)
    assert arguments["limits"]["aspace_limit_kb"] == 4096
    assert arguments["limits"]["file_size_limit_kb"] == 2048
    assert arguments["limits"]["time_limit_ms"] == 1000
    assert arguments["limits"]["wall_time_ms"] == 2000
    assert arguments["limits"]["memory_limit_mb"] == 64
    assert storage.exists(arguments["compiles"]["solution"])
    assert storage.exists(arguments["compiles"]["checker"])
    assert len(arguments["tests"]) == 1
    assert arguments["tests"][0]["name"] == "sample.in"
    assert storage.exists(arguments["tests"][0]["input_digest"])
    assert storage.exists(arguments["tests"][0]["output_digest"])


def test_make_input_digest_aspace_none_and_file_size_default(
    storage: FileStorage,
    test_assets_path: Path,
    tmp_path: Path,
):
    configuration = _configuration(tmp_path, aspace_limit=None, file_size_limit=None)
    input_digest = _make_digest(storage, test_assets_path, configuration, tmp_path)

    arguments = storage.download_dictionary(input_digest)
    assert arguments["limits"]["aspace_limit_kb"] is None
    assert arguments["limits"]["file_size_limit_kb"] == 16 * 1024


def test_parse_arguments_roundtrip(
    storage: FileStorage,
    test_assets_path: Path,
    tmp_path: Path,
):
    configuration = _configuration(tmp_path, aspace_limit=8192, file_size_limit=1024)
    input_digest = _make_digest(storage, test_assets_path, configuration, tmp_path)
    pipeline = ProblemTestingPipeline()
    context = PipelineContext(input_digest=input_digest, storage=storage)

    arguments = pipeline._parse_arguments(context)

    assert arguments.limits.aspace_limit_kb == 8192
    assert arguments.limits.file_size_limit_kb == 1024
    assert arguments.limits.time_limit_ms == 1000
    assert arguments.limits.wall_time_ms == 2000
    assert arguments.limits.memory_limit_mb == 64
    assert len(arguments.tests) == 1
    assert arguments.tests[0].name == "sample.in"
    assert arguments.binary_names[ProgramRole.SOLUTION] == "hello"
    assert arguments.binary_names[ProgramRole.CHECKER] == "exact_checker"
    assert arguments.problem_name is None


def test_issued_jobs_carry_display_labels(
    storage: FileStorage,
    test_assets_path: Path,
    tmp_path: Path,
):
    configuration = _configuration(tmp_path)
    input_digest = _make_digest(
        storage,
        test_assets_path,
        configuration,
        tmp_path,
        problem_name="aplusb",
    )
    pipeline = ProblemTestingPipeline()
    context = PipelineContext(input_digest=input_digest, storage=storage)
    args = pipeline._parse_arguments(context)

    compile_job = pipeline.get_next_job(context)
    assert isinstance(compile_job, ScheduledJob)
    assert compile_job.job.labels == {"Problem": "aplusb", "Binary": "hello"}

    checker_job = pipeline.get_next_job(context)
    assert isinstance(checker_job, ScheduledJob)
    assert checker_job.job.labels == {"Problem": "aplusb", "Binary": "exact_checker"}

    assert args.labels(ProgramRole.SOLUTION, "tests/sample-01.txt") == {
        "Problem": "aplusb",
        "Test": "sample-01.txt",
        "Binary": "hello",
    }
    assert args.labels(ProgramRole.CHECKER, "sample.in") == {
        "Problem": "aplusb",
        "Test": "sample.in",
        "Binary": "exact_checker",
    }


@pytest.mark.slow
def test_run_job_gets_aspace_and_file_size_limits(
    storage: FileStorage,
    isolate_factory: IsolateFactory,
    test_assets_path: Path,
    tmp_path: Path,
):
    configuration = _configuration(tmp_path, aspace_limit=2048, file_size_limit=512)
    input_digest = _make_digest(storage, test_assets_path, configuration, tmp_path)
    pipeline = ProblemTestingPipeline()
    context = PipelineContext(
        input_digest=input_digest,
        storage=storage,
        isolate_factory=isolate_factory,
    )

    compile_job = pipeline.get_next_job(context)
    assert isinstance(compile_job, ScheduledJob)
    compile_job.job.execute(context.job_context(compile_job.input_digest))
    checker_job = pipeline.get_next_job(context)
    assert isinstance(checker_job, ScheduledJob)
    checker_job.job.execute(context.job_context(checker_job.input_digest))
    run_job = pipeline.get_next_job(context)
    assert isinstance(run_job, ScheduledJob)

    run_arguments = storage.download_dictionary(run_job.input_digest)
    assert run_arguments["aspace_limit_kb"] == 2048
    assert run_arguments["file_size_limit_kb"] == 512


@pytest.mark.slow
def test_problem_testing_success(
    storage: FileStorage,
    isolate_factory: IsolateFactory,
    test_assets_path: Path,
    tmp_path: Path,
):
    configuration = _configuration(tmp_path)
    input_digest = _make_digest(storage, test_assets_path, configuration, tmp_path)
    pipeline = ProblemTestingPipeline()
    run_pipeline(
        pipeline,
        PipelineContext(
            input_digest=input_digest,
            storage=storage,
            isolate_factory=isolate_factory,
        ),
    )

    assert pipeline.get_status() is PipelineStatus.COMPLETED
    result = pipeline.get_result(storage)
    assert result is not None
    assert result.verdict is Verdict.OK
    assert ProgramRole.SOLUTION in result.compile_digests
    assert len(result.tests) == 1
    assert result.tests[0].verdict is Verdict.OK
    assert result.tests[0].check_digest is not None


@pytest.mark.slow
def test_problem_testing_wrong_answer(
    storage: FileStorage,
    isolate_factory: IsolateFactory,
    test_assets_path: Path,
    tmp_path: Path,
):
    configuration = _configuration(tmp_path, expected=b"nope\n")
    input_digest = _make_digest(storage, test_assets_path, configuration, tmp_path)
    pipeline = ProblemTestingPipeline()
    run_pipeline(
        pipeline,
        PipelineContext(
            input_digest=input_digest,
            storage=storage,
            isolate_factory=isolate_factory,
        ),
    )

    result = pipeline.get_result(storage)
    assert pipeline.get_status() is PipelineStatus.COMPLETED
    assert result is not None
    assert result.verdict is Verdict.WA
    assert result.tests[0].verdict is Verdict.WA


@pytest.mark.slow
def test_problem_testing_compilation_error(
    storage: FileStorage,
    isolate_factory: IsolateFactory,
    test_assets_path: Path,
    tmp_path: Path,
):
    gxx = shutil.which("g++")
    if gxx is None:
        pytest.skip("g++ is not available")

    configuration = _configuration(tmp_path)
    input_digest = ProblemTestingPipeline.make_input_digest(
        storage,
        programs={
            ProgramRole.SOLUTION: CompileSpec(
                sources=[(b"int main() { syntax error", "main.cpp")],
                command=[gxx, "-O2", "-o", "solution", 0],
            ),
            ProgramRole.CHECKER: CompileSpec(
                sources=[(test_assets_path / "exact_checker.cpp", "checker.cpp")],
                command=[gxx, "-O2", "-o", "checker", 0],
            ),
        },
        configuration=configuration,
        problem_dir=tmp_path,
    )
    pipeline = ProblemTestingPipeline()
    run_pipeline(
        pipeline,
        PipelineContext(
            input_digest=input_digest,
            storage=storage,
            isolate_factory=isolate_factory,
        ),
    )

    assert pipeline.get_status() is PipelineStatus.COMPLETED
    result = pipeline.get_result(storage)
    assert result is not None
    assert result.verdict is Verdict.CE
    assert result.tests == []
    solution_compile = result.compile_digests[ProgramRole.SOLUTION]
    compile_data = storage.download_dictionary(solution_compile)
    assert IsolateMeta(**compile_data["meta"]).exitcode != 0


def test_problem_testing_missing_answer_requires_model(
    storage: FileStorage,
    test_assets_path: Path,
    tmp_path: Path,
):
    (tmp_path / "sample.in").write_bytes(b"")
    configuration = ProblemConfiguration(
        time_limit=1000,
        memory_limit=64,
        file_size_limit=2048,
        input_file_name="input.txt",
        output_file_name="output.txt",
        samples=[ProblemTest(input_file="sample.in")],
        tests=[],
        solutions=[],
        sources=[],
    )
    with pytest.raises(ValueError, match="model solution is required"):
        _make_digest(storage, test_assets_path, configuration, tmp_path)


def test_store_test_rejects_path_escape(
    storage: FileStorage,
    test_assets_path: Path,
    tmp_path: Path,
):
    gxx = shutil.which("g++")
    if gxx is None:
        pytest.skip("g++ is not available")
    (tmp_path / "sample.in").write_bytes(b"")
    secret = tmp_path.parent / "secret.txt"
    secret.write_text("worker-secret")
    configuration = ProblemConfiguration(
        time_limit=1000,
        memory_limit=64,
        input_file_name="input.txt",
        output_file_name="output.txt",
        samples=[ProblemTest(input_file="../secret.txt")],
        tests=[],
        solutions=[],
        sources=[],
    )
    with pytest.raises(ValueError, match=r"\.\.|relative"):
        ProblemTestingPipeline.make_input_digest(
            storage,
            programs={
                ProgramRole.SOLUTION: CompileSpec(
                    sources=[(test_assets_path / "hello.cpp", "main.cpp")],
                    command=[gxx, "-O2", "-o", "solution", 0],
                ),
                ProgramRole.CHECKER: CompileSpec(
                    sources=[
                        (test_assets_path / "exact_checker.cpp", "checker.cpp")
                    ],
                    command=[gxx, "-O2", "-o", "checker", 0],
                ),
            },
            configuration=configuration,
            problem_dir=tmp_path,
        )


def test_store_test_rejects_symlink(
    storage: FileStorage,
    test_assets_path: Path,
    tmp_path: Path,
):
    gxx = shutil.which("g++")
    if gxx is None:
        pytest.skip("g++ is not available")
    secret = tmp_path / "outside.txt"
    secret.write_text("secret")
    # place symlink as the sample, pointing at a file we still allow inside dir —
    # use a symlink to a file outside problem_dir
    outside = tmp_path.parent / "host-secret.txt"
    outside.write_text("host-secret")
    (tmp_path / "sample.in").symlink_to(outside)
    configuration = ProblemConfiguration(
        time_limit=1000,
        memory_limit=64,
        input_file_name="input.txt",
        output_file_name="output.txt",
        samples=[ProblemTest(input_file="sample.in")],
        tests=[],
        solutions=[],
        sources=[],
    )
    with pytest.raises(ValueError, match="symlink"):
        ProblemTestingPipeline.make_input_digest(
            storage,
            programs={
                ProgramRole.SOLUTION: CompileSpec(
                    sources=[(test_assets_path / "hello.cpp", "main.cpp")],
                    command=[gxx, "-O2", "-o", "solution", 0],
                ),
                ProgramRole.CHECKER: CompileSpec(
                    sources=[
                        (test_assets_path / "exact_checker.cpp", "checker.cpp")
                    ],
                    command=[gxx, "-O2", "-o", "checker", 0],
                ),
            },
            configuration=configuration,
            problem_dir=tmp_path,
        )


def test_parse_gen_argv_drops_binary_name():
    assert parse_gen_argv("gen -c=1e3") == ["-c=1e3"]
    assert parse_gen_argv("gen -c 1e3") == ["-c", "1e3"]
    assert parse_gen_argv("gen --seed 42 -c 10") == ["--seed", "42", "-c", "10"]


def test_parse_gen_argv_rejects_empty_and_flag_first():
    with pytest.raises(ValueError, match="empty gen_cmd"):
        parse_gen_argv("")
    with pytest.raises(ValueError, match="generator name"):
        parse_gen_argv("-c 1e3")


def test_problem_test_requires_exactly_one_of_input_or_gen():
    with pytest.raises(ValidationError, match="exactly one of input_file or gen_cmd"):
        ProblemTest()
    with pytest.raises(ValidationError, match="exactly one of input_file or gen_cmd"):
        ProblemTest(input_file="a.in", gen_cmd="gen -c 1", name="a.txt")
    with pytest.raises(ValidationError, match="generated tests require name"):
        ProblemTest(gen_cmd="gen -c 1")
    with pytest.raises(ValidationError, match="generated tests cannot have output_file"):
        ProblemTest(gen_cmd="gen -c 1", name="a.txt", output_file="a.out")


def test_problem_testing_arguments_requires_generator_for_gen_cmd():
    digest = "a" * 64
    limits = RunLimits(
        time_limit_ms=1000,
        wall_time_ms=2000,
        memory_limit_mb=64,
        aspace_limit_kb=None,
        file_size_limit_kb=2048,
    )
    with pytest.raises(ValidationError, match="generator is required"):
        ProblemTestingArguments(
            compiles={
                ProgramRole.SOLUTION: digest,
                ProgramRole.CHECKER: digest,
                ProgramRole.MODEL: digest,
            },
            limits=limits,
            tests=[ProblemTestingCase(name="random-01.txt", gen_argv=["-c", "1e3"])],
        )


def test_problem_configuration_rejects_zero_memory():
    with pytest.raises(ValidationError, match="memory_limit"):
        ProblemConfiguration(
            time_limit=1000,
            memory_limit=0,
            input_file_name="input.txt",
            output_file_name="output.txt",
            samples=[],
            tests=[],
            solutions=[],
            sources=[],
        )


def _extra_hello_test(problem_dir: Path) -> ProblemTest:
    (problem_dir / "2.in").write_bytes(b"")
    (problem_dir / "2.out").write_bytes(b"Hello, output world!\n")
    return ProblemTest(input_file="2.in", output_file="2.out")


def _after_compiles(
    pipeline: ProblemTestingPipeline,
    context: PipelineContext,
) -> None:
    for _ in range(2):
        job = pipeline.get_next_job(context)
        assert isinstance(job, ScheduledJob)
        job.job.execute(context.job_context(job.input_digest))


@pytest.mark.slow
def test_checker_is_issued_before_next_test_run(
    storage: FileStorage,
    isolate_factory: IsolateFactory,
    test_assets_path: Path,
    tmp_path: Path,
):
    configuration = _configuration(tmp_path, extra_tests=[_extra_hello_test(tmp_path)])
    input_digest = _make_digest(storage, test_assets_path, configuration, tmp_path)
    pipeline = ProblemTestingPipeline()
    context = PipelineContext(
        input_digest=input_digest,
        storage=storage,
        isolate_factory=isolate_factory,
    )
    _after_compiles(pipeline, context)

    run0 = pipeline.get_next_job(context)
    assert isinstance(run0, ScheduledJob)
    assert run0.job.job_id == "run-0"
    run0.job.execute(context.job_context(run0.input_digest))

    check0 = pipeline.get_next_job(context)
    assert isinstance(check0, ScheduledJob)
    assert check0.job.job_id == "check-0"
    assert isinstance(check0.job, ToolRunJob)


@pytest.mark.slow
def test_next_unrun_test_starts_while_previous_run_is_in_flight(
    storage: FileStorage,
    isolate_factory: IsolateFactory,
    test_assets_path: Path,
    tmp_path: Path,
):
    configuration = _configuration(tmp_path, extra_tests=[_extra_hello_test(tmp_path)])
    input_digest = _make_digest(storage, test_assets_path, configuration, tmp_path)
    pipeline = ProblemTestingPipeline()
    context = PipelineContext(
        input_digest=input_digest,
        storage=storage,
        isolate_factory=isolate_factory,
    )
    _after_compiles(pipeline, context)

    run0 = pipeline.get_next_job(context)
    assert isinstance(run0, ScheduledJob)
    assert isinstance(run0.job, SimpleRunJob)
    assert run0.job.job_id == "run-0"

    run1 = pipeline.get_next_job(context)
    assert isinstance(run1, ScheduledJob)
    assert isinstance(run1.job, SimpleRunJob)
    assert run1.job.job_id == "run-1"
    assert pipeline.get_next_job(context) is NextJobStatus.WAITING


@pytest.mark.slow
def test_problem_testing_two_tests_success(
    storage: FileStorage,
    isolate_factory: IsolateFactory,
    test_assets_path: Path,
    tmp_path: Path,
):
    configuration = _configuration(tmp_path, extra_tests=[_extra_hello_test(tmp_path)])
    input_digest = _make_digest(storage, test_assets_path, configuration, tmp_path)
    pipeline = ProblemTestingPipeline()
    run_pipeline(
        pipeline,
        PipelineContext(
            input_digest=input_digest,
            storage=storage,
            isolate_factory=isolate_factory,
        ),
    )

    result = pipeline.get_result(storage)
    assert pipeline.get_status() is PipelineStatus.COMPLETED
    assert result is not None
    assert result.verdict is Verdict.OK
    assert len(result.tests) == 2
    assert [test.verdict for test in result.tests] == [Verdict.OK, Verdict.OK]
    assert all(test.check_digest is not None for test in result.tests)
