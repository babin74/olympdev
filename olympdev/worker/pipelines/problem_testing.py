from pathlib import Path
from typing import Callable, Self

from pydantic import model_validator

from olympdev.problem.types import (
    ProblemConfiguration,
    ProblemScoringMode,
    ProblemTest,
    parse_gen_argv,
    parse_valid_argv,
    resolve_problem_file,
)
from olympdev.schema import FrozenModel, StorageModel
from olympdev.settings import get_settings
from olympdev.storage import AbstractStorage
from olympdev.worker.job import JobStatus
from olympdev.worker.jobs.compile import CompileJob, CompileJobResult, compile_job_class
from olympdev.worker.pipeline import (
    AbstractPipeline,
    NextJobStatus,
    PipelineContext,
    ScheduledJob,
    ScheduledPipeline,
)
from olympdev.worker.pipelines.problem_test import (
    ProblemTestArguments,
    ProblemTestPipeline,
    ProblemTestResult,
    ProblemTestingCase,
    RunLimits,
    TestingContext,
)
from olympdev.worker.roles import (
    REQUIRED_ROLES,
    ROLE_ORDER,
    ROLE_SPECS,
    ProgramRole,
)
from olympdev.worker.verdict import Verdict

_IN_FLIGHT = (JobStatus.PENDING, JobStatus.RUNNING)
_SOURCE_SUFFIXES = {".c", ".cc", ".cpp", ".cxx"}

ProblemTestingTestResult = ProblemTestResult


def _in_flight(job: ScheduledJob | None) -> bool:
    return job is not None and job.job.get_status() in _IN_FLIGHT


def _compile_result(
    storage: AbstractStorage, job: ScheduledJob
) -> CompileJobResult | None:
    compile_job = job.job
    assert isinstance(compile_job, CompileJob)
    return compile_job.get_result(storage)


class ProblemTestingArguments(TestingContext, StorageModel):
    #: Compile-job input digest per role. Roles the problem does not use are
    #: simply absent.
    compiles: dict[ProgramRole, str]
    tests: list[ProblemTestingCase]

    @model_validator(mode="after")
    def _check_tests(self) -> Self:
        if len(self.tests) > get_settings().limits.max_tests_per_problem:
            raise ValueError(f"too many tests: {len(self.tests)}")
        missing = REQUIRED_ROLES - set(self.compiles)
        if missing:
            raise ValueError(
                "missing programs: "
                + ", ".join(sorted(role.value for role in missing))
            )
        if (
            any(test.output_digest is None for test in self.tests)
            and ProgramRole.MODEL not in self.compiles
        ):
            raise ValueError(
                "model solution is required when tests have no output file"
            )
        if (
            any(test.gen_argv is not None for test in self.tests)
            and ProgramRole.GENERATOR not in self.compiles
        ):
            raise ValueError("generator is required when tests use gen_cmd")
        return self

    def roles(self) -> list[ProgramRole]:
        """Roles this problem uses, in a stable order."""
        return [role for role in ROLE_ORDER if role in self.compiles]


class ProblemTestingResult(StorageModel):
    verdict: Verdict
    #: Compile-job output digest per role.
    compile_digests: dict[ProgramRole, str]
    tests: list[ProblemTestResult]


class CompileSpec(FrozenModel):
    """Sources plus the command that turns them into one role's binary."""

    sources: list[tuple[Path | bytes, str | Path]]
    command: list[str | int]

    model_config = FrozenModel.model_config | {"arbitrary_types_allowed": True}

    def binary_display_name(self) -> str | None:
        """Stem of the first C/C++ source, for job labels."""
        for source, sandbox_path in self.sources:
            path = source if isinstance(source, Path) else Path(sandbox_path)
            if path.suffix.lower() in _SOURCE_SUFFIXES:
                return path.stem
        return None


def _resolve_valid_argv(test: ProblemTest, configuration: ProblemConfiguration) -> list[str]:
    """Argv for the validator on this test - see ProblemTest.valid_cmd.

    An explicit ``valid_cmd`` always wins outright. Otherwise, under
    ``mode: "group-points"``, a test defaults to ``--group <group_id>`` (two
    tokens) so it can enforce that group's constraints without every test
    spelling it out - this is testlib's own built-in convention:
    ``registerValidation(argc, argv)`` already parses ``--group <value>`` and
    exposes it via ``validator.group()``, no custom argv parsing needed on
    the validator's side.
    """
    if test.valid_cmd is not None:
        return parse_valid_argv(test.valid_cmd)
    if configuration.mode is ProblemScoringMode.GROUP_POINTS and test.group_id is not None:
        return ["--group", str(test.group_id)]
    return []


def _store_test(
    storage: AbstractStorage,
    problem_dir: Path,
    test: ProblemTest,
    configuration: ProblemConfiguration,
) -> ProblemTestingCase:
    output_digest = None
    if test.output_file is not None:
        output_digest = storage.upload_file(
            resolve_problem_file(problem_dir, test.output_file)
        )
    valid_argv = _resolve_valid_argv(test, configuration)
    if test.gen_cmd is not None:
        return ProblemTestingCase(
            name=test.display_name(),
            output_digest=output_digest,
            gen_argv=parse_gen_argv(test.gen_cmd),
            valid_argv=valid_argv,
        )
    assert test.input_file is not None
    return ProblemTestingCase(
        name=test.display_name(),
        input_digest=storage.upload_file(
            resolve_problem_file(problem_dir, test.input_file)
        ),
        output_digest=output_digest,
        valid_argv=valid_argv,
    )


def _compile_step(role: ProgramRole) -> str:
    return f"compile-{role.value}"


class ProblemTestingPipeline(AbstractPipeline):
    """Compile every role the problem uses, then run each test as a child pipeline."""

    pipeline_type = "problem_testing"

    def __init__(self, pipeline_id: str = "problem-testing"):
        super().__init__(pipeline_id)
        # Caches only: both are re-derived from storage after a restore.
        self._arguments: ProblemTestingArguments | None = None
        self._programs_cache: dict[ProgramRole, str] | None = None

    @classmethod
    def make_input_digest(
        cls,
        storage: AbstractStorage,
        *,
        programs: dict[ProgramRole, CompileSpec],
        configuration: ProblemConfiguration,
        problem_dir: Path,
        problem_name: str | None = None,
    ) -> str:
        """Upload every role's compile input plus the tests, and return the digest."""
        compiles: dict[ProgramRole, str] = {}
        binary_names: dict[ProgramRole, str] = {}
        for role in ROLE_ORDER:
            spec = programs.get(role)
            if spec is None:
                continue
            compiles[role] = compile_job_class(role).make_input_digest(
                storage, sources=spec.sources, command=spec.command
            )
            display_name = spec.binary_display_name()
            if display_name is not None:
                binary_names[role] = display_name
        return ProblemTestingArguments(
            compiles=compiles,
            limits=RunLimits.from_configuration(configuration),
            problem_name=problem_name,
            binary_names=binary_names,
            tests=[
                _store_test(storage, problem_dir, test, configuration)
                for test in [*configuration.samples, *configuration.tests]
            ],
        ).upload(storage)

    def _parse_arguments(self, context: PipelineContext) -> ProblemTestingArguments:
        if self._arguments is None:
            args = ProblemTestingArguments.require_from_storage(
                context.storage,
                context.input_digest,
                missing="missing pipeline input",
            )
            for digest in args.compiles.values():
                context.storage.validate_digest(digest)
            for test in args.tests:
                for digest in (test.input_digest, test.output_digest):
                    if digest is not None:
                        context.storage.validate_digest(digest)
            self._arguments = args
        return self._arguments

    def get_result(self, storage: AbstractStorage) -> ProblemTestingResult | None:
        digest = self.get_output_digest()
        if digest is None:
            return None
        return ProblemTestingResult.from_storage(storage, digest)

    def _next_job(self, context: PipelineContext) -> ScheduledJob | NextJobStatus:
        args = self._parse_arguments(context)
        storage = context.storage

        compile_step = self._step_compiles(context, args)
        if compile_step is not None:
            return compile_step

        tests = self._ensure_tests(storage, args)

        # Prefer finishing a test that already has the participant's answer,
        # then start tests that have not run, then drain whatever is left.
        stages: tuple[Callable[[ProblemTestPipeline], bool], ...] = (
            lambda child: child.has_participant_answer(storage),
            lambda child: child.run_not_started(),
        )
        for ready in stages:
            nxt = self._pull_first(tests, context, ready)
            if nxt is not None:
                return nxt

        if any(self._child(s).is_busy() for s in tests):
            return NextJobStatus.WAITING

        nxt = self._pull_first(tests, context, lambda child: not child.is_finished())
        if nxt is not None:
            return nxt

        return self._complete_overall(storage)

    # --- compiles -------------------------------------------------------

    def _step_compiles(
        self, context: PipelineContext, args: ProblemTestingArguments
    ) -> ScheduledJob | NextJobStatus | None:
        if self._programs_cache is not None:
            return None
        for role in args.roles():
            step = _compile_step(role)
            if not self.has_step(step):
                return self.issue(
                    step,
                    compile_job_class(role),
                    args.compiles[role],
                    labels=args.labels(role),
                )
        if self.any_step_in_flight():
            return NextJobStatus.WAITING
        return self._finish_compiles(context.storage, args)

    def _finish_compiles(
        self, storage: AbstractStorage, args: ProblemTestingArguments
    ) -> NextJobStatus | None:
        programs: dict[ProgramRole, str] = {}
        for role in args.roles():
            scheduled = self.step(_compile_step(role))
            assert scheduled is not None
            result = _compile_result(storage, scheduled)
            if scheduled.job.get_status() is JobStatus.FAILED:
                if result is None or result.meta.is_sandbox_error():
                    self.fail(scheduled.job.get_failure_reason())
                else:
                    self._complete_with(
                        storage, ROLE_SPECS[role].compile_failure_verdict, []
                    )
                return NextJobStatus.FINISHED
            if result is None or result.program_digest is None:
                self.fail(f"{ROLE_SPECS[role].label} compilation produced no program")
                return NextJobStatus.FINISHED
            programs[role] = result.program_digest
        self._programs_cache = programs
        return None

    # --- per-test children ----------------------------------------------

    def _ensure_tests(
        self, storage: AbstractStorage, args: ProblemTestingArguments
    ) -> list[ScheduledPipeline]:
        if not self._children:
            assert self._programs_cache is not None
            for index, case in enumerate(args.tests):
                self.add_child(
                    f"test-{index}",
                    ProblemTestPipeline(f"test-{index}"),
                    ProblemTestPipeline.make_input_digest(
                        storage,
                        ProblemTestArguments.from_parent(
                            args, case, programs=self._programs_cache, index=index
                        ),
                    ),
                )
        return self.children()

    def _child(self, scheduled: ScheduledPipeline) -> ProblemTestPipeline:
        pipeline = scheduled.pipeline
        assert isinstance(pipeline, ProblemTestPipeline)
        return pipeline

    def _pull_first(
        self,
        tests: list[ScheduledPipeline],
        context: PipelineContext,
        ready: Callable[[ProblemTestPipeline], bool],
    ) -> ScheduledJob | NextJobStatus | None:
        for scheduled in tests:
            if not ready(self._child(scheduled)):
                continue
            job = self.pull_child(scheduled, context)
            if job is not None:
                return job
            if self.is_finished():
                return NextJobStatus.FINISHED
        return None

    # --- results ---------------------------------------------------------

    def _compile_digests(self) -> dict[ProgramRole, str]:
        digests: dict[ProgramRole, str] = {}
        for role in ROLE_ORDER:
            scheduled = self.step(_compile_step(role))
            if scheduled is None:
                continue
            digest = scheduled.job.get_output_digest()
            if digest is not None:
                digests[role] = digest
        return digests

    def _complete_with(
        self,
        storage: AbstractStorage,
        verdict: Verdict,
        tests: list[ProblemTestResult],
    ) -> None:
        self.complete(
            ProblemTestingResult(
                verdict=verdict,
                compile_digests=self._compile_digests(),
                tests=tests,
            ).upload(storage)
        )

    def _complete_overall(self, storage: AbstractStorage) -> NextJobStatus:
        test_results: list[ProblemTestResult] = []
        overall = Verdict.OK
        for scheduled in self.children():
            result = self._child(scheduled).get_result(storage)
            if result is None:
                self.fail(f"missing test result for {scheduled.pipeline.pipeline_id}")
                return NextJobStatus.FINISHED
            test_results.append(result)
            if overall is Verdict.OK and result.verdict is not Verdict.OK:
                overall = result.verdict
        self._complete_with(storage, overall, test_results)
        return NextJobStatus.FINISHED
