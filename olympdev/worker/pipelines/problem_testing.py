from pathlib import Path
from typing import Callable, Self

from pydantic import model_validator

from olympdev.problem.types import (
    ProblemConfiguration,
    ProblemTest,
    parse_gen_argv,
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


def _store_test(
    storage: AbstractStorage,
    problem_dir: Path,
    test: ProblemTest,
) -> ProblemTestingCase:
    output_digest = None
    if test.output_file is not None:
        output_digest = storage.upload_file(
            resolve_problem_file(problem_dir, test.output_file)
        )
    if test.gen_cmd is not None:
        return ProblemTestingCase(
            name=test.display_name(),
            output_digest=output_digest,
            gen_argv=parse_gen_argv(test.gen_cmd),
        )
    assert test.input_file is not None
    return ProblemTestingCase(
        name=test.display_name(),
        input_digest=storage.upload_file(
            resolve_problem_file(problem_dir, test.input_file)
        ),
        output_digest=output_digest,
    )


class ProblemTestingPipeline(AbstractPipeline):
    """Compile every role the problem uses, then run each test as a child pipeline."""

    def __init__(self, pipeline_id: str = "problem-testing"):
        super().__init__(pipeline_id)
        self._arguments: ProblemTestingArguments | None = None
        self._compiles: dict[ProgramRole, ScheduledJob] = {}
        self._programs: dict[ProgramRole, str] | None = None
        self._tests: list[ScheduledPipeline] | None = None

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
                _store_test(storage, problem_dir, test)
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
        if self._programs is not None:
            return None
        for role in args.roles():
            if role not in self._compiles:
                scheduled = ScheduledJob(
                    job=compile_job_class(role)(
                        f"compile-{role.value}",
                        labels=args.labels(role),
                    ),
                    input_digest=args.compiles[role],
                )
                self._compiles[role] = scheduled
                return scheduled
        if any(_in_flight(job) for job in self._compiles.values()):
            return NextJobStatus.WAITING
        return self._finish_compiles(context.storage, args)

    def _finish_compiles(
        self, storage: AbstractStorage, args: ProblemTestingArguments
    ) -> NextJobStatus | None:
        programs: dict[ProgramRole, str] = {}
        for role in args.roles():
            scheduled = self._compiles[role]
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
        self._programs = programs
        return None

    # --- per-test children ----------------------------------------------

    def _ensure_tests(
        self, storage: AbstractStorage, args: ProblemTestingArguments
    ) -> list[ScheduledPipeline]:
        if self._tests is None:
            assert self._programs is not None
            self._tests = [
                ScheduledPipeline(
                    pipeline=ProblemTestPipeline(f"test-{index}"),
                    input_digest=ProblemTestPipeline.make_input_digest(
                        storage,
                        ProblemTestArguments.from_parent(
                            args, case, programs=self._programs, index=index
                        ),
                    ),
                )
                for index, case in enumerate(args.tests)
            ]
        return self._tests

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
        return {
            role: digest
            for role, scheduled in self._compiles.items()
            if (digest := scheduled.job.get_output_digest()) is not None
        }

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
        assert self._tests is not None
        test_results: list[ProblemTestResult] = []
        overall = Verdict.OK
        for scheduled in self._tests:
            result = self._child(scheduled).get_result(storage)
            if result is None:
                self.fail(f"missing test result for {scheduled.pipeline.pipeline_id}")
                return NextJobStatus.FINISHED
            test_results.append(result)
            if overall is Verdict.OK and result.verdict is not Verdict.OK:
                overall = result.verdict
        self._complete_with(storage, overall, test_results)
        return NextJobStatus.FINISHED
