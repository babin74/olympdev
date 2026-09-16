"""Validator argv: ProblemTest.valid_cmd, and its default under group-points.

See CLAUDE.md: Adding a job/pipeline and roi-rounds/discovered/rules.md §9
("Валидатор ограничений на каждую группу"). Mirrors gen_cmd's existing plumbing
(tests/test_problem_testing_pipeline.py's _configuration/_make_digest), but a
validator's binary is separate (program_digest), so there is no leading token
to drop the way parse_gen_argv drops gen_cmd's.
"""

import shutil
from pathlib import Path

import pytest
from pydantic import ValidationError

from olympdev.isolate import IsolateFactory
from olympdev.resources import asset_file
from olympdev.problem.types import (
    ProblemConfiguration,
    ProblemScoringMode,
    ProblemSource,
    ProblemSourceType,
    ProblemTest,
    parse_valid_argv,
)
from olympdev.storage.filestorage import FileStorage
from olympdev.worker.jobs.tool_run import ToolRunJob
from olympdev.worker.pipeline import PipelineContext, PipelineStatus, ScheduledJob, run_pipeline
from olympdev.worker.pipelines.problem_testing import CompileSpec, ProblemTestingPipeline
from olympdev.worker.roles import ProgramRole
from olympdev.worker.verdict import Verdict

pytestmark = pytest.mark.slow


def test_parse_valid_argv_splits_every_token():
    assert parse_valid_argv("--seed=1") == ["--seed=1"]
    assert parse_valid_argv("--group 1 --seed 42") == ["--group", "1", "--seed", "42"]


def test_parse_valid_argv_rejects_empty():
    with pytest.raises(ValueError, match="empty valid_cmd"):
        parse_valid_argv("")


def test_problem_test_rejects_malformed_valid_cmd():
    with pytest.raises(ValidationError):
        ProblemTest(input_file="a.in", valid_cmd="'unterminated")


def _configuration(problem_dir: Path, *, group_id: int, valid_cmd: str | None, value: int) -> ProblemConfiguration:
    test_file = problem_dir / f"g{group_id}.in"
    test_file.write_text(f"{value}\n")
    output_file = problem_dir / f"g{group_id}.out"
    output_file.write_text("Hello, output world!\n")
    return ProblemConfiguration(
        time_limit_ms=1000,
        memory_limit_mb=64,
        mode=ProblemScoringMode.GROUP_POINTS,
        groups=[
            {"id": 1, "points": 40, "depends_on": []},
            {"id": 2, "points": 60, "depends_on": []},
        ],
        sources=[ProblemSource(file="checker.cpp", type=ProblemSourceType.CHECK)],
        samples=[],
        tests=[
            ProblemTest(
                input_file=test_file.name,
                output_file=output_file.name,
                group_id=group_id,
                valid_cmd=valid_cmd,
            )
        ],
    )


def _make_digest(
    storage: FileStorage,
    test_assets_path: Path,
    configuration: ProblemConfiguration,
    problem_dir: Path,
) -> str:
    gxx = shutil.which("g++")
    if gxx is None:
        pytest.skip("g++ is not available")
    (problem_dir / "checker.cpp").write_bytes(
        (test_assets_path / "exact_checker.cpp").read_bytes()
    )
    with asset_file("testlib.h") as testlib_path:
        return ProblemTestingPipeline.make_input_digest(
            storage,
            programs={
                ProgramRole.SOLUTION: CompileSpec(
                    sources=[(test_assets_path / "hello.cpp", "main.cpp")],
                    command=[gxx, "-O2", "-o", "solution", 0],
                ),
                ProgramRole.CHECKER: CompileSpec(
                    sources=[(problem_dir / "checker.cpp", "checker.cpp")],
                    command=[gxx, "-O2", "-o", "checker", 0],
                ),
                ProgramRole.VALIDATOR: CompileSpec(
                    sources=[
                        (test_assets_path / "group_validator.cpp", "valid.cpp"),
                        (testlib_path, "testlib.h"),
                    ],
                    command=[gxx, "-O2", "-o", "valid", 0],
                ),
            },
            configuration=configuration,
            problem_dir=problem_dir,
        )


def _execute_compiles(pipeline: ProblemTestingPipeline, context: PipelineContext) -> None:
    for _ in range(3):
        job = pipeline.get_next_job(context)
        assert isinstance(job, ScheduledJob)
        job.job.execute(context.job_context(job.input_digest))


def test_group_points_defaults_valid_cmd_to_group_flag(
    storage: FileStorage, isolate_factory: IsolateFactory, test_assets_path: Path, tmp_path: Path
):
    configuration = _configuration(tmp_path, group_id=1, valid_cmd=None, value=5)
    input_digest = _make_digest(storage, test_assets_path, configuration, tmp_path)
    pipeline = ProblemTestingPipeline()
    context = PipelineContext(
        input_digest=input_digest, storage=storage, isolate_factory=isolate_factory
    )
    _execute_compiles(pipeline, context)

    valid0 = pipeline.get_next_job(context)
    assert isinstance(valid0, ScheduledJob)
    assert isinstance(valid0.job, ToolRunJob)
    arguments = storage.download_dictionary(valid0.input_digest)
    assert arguments["command"] == ["--group", "1"]


def test_explicit_valid_cmd_overrides_group_default(
    storage: FileStorage, isolate_factory: IsolateFactory, test_assets_path: Path, tmp_path: Path
):
    configuration = _configuration(tmp_path, group_id=1, valid_cmd="--group 2", value=5)
    input_digest = _make_digest(storage, test_assets_path, configuration, tmp_path)
    pipeline = ProblemTestingPipeline()
    context = PipelineContext(
        input_digest=input_digest, storage=storage, isolate_factory=isolate_factory
    )
    _execute_compiles(pipeline, context)

    valid0 = pipeline.get_next_job(context)
    assert isinstance(valid0, ScheduledJob)
    arguments = storage.download_dictionary(valid0.input_digest)
    assert arguments["command"] == ["--group", "2"]


def test_default_group_argv_is_enforced_by_the_validator(
    storage: FileStorage, isolate_factory: IsolateFactory, test_assets_path: Path, tmp_path: Path
):
    """group_validator.cpp caps group 1 at [-10, 10] - a value that only
    fits in group 2's wider range must fail validation once its test carries
    group_id=1 and no explicit valid_cmd (so the default --group 1 applies)."""
    configuration = _configuration(tmp_path, group_id=1, valid_cmd=None, value=500)
    input_digest = _make_digest(storage, test_assets_path, configuration, tmp_path)
    pipeline = ProblemTestingPipeline()
    run_pipeline(
        pipeline,
        PipelineContext(input_digest=input_digest, storage=storage, isolate_factory=isolate_factory),
    )
    assert pipeline.get_status() is PipelineStatus.COMPLETED
    result = pipeline.get_result(storage)
    assert result is not None
    assert result.verdict is Verdict.CF
