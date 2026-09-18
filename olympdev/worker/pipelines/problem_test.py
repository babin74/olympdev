from pathlib import Path
from typing import Any, Self

from pydantic import model_validator

from olympdev.isolate import IsolateLimits, IsolateMeta
from olympdev.problem.types import ProblemConfiguration
from olympdev.schema import FrozenModel, StorageModel
from olympdev.storage import AbstractStorage
from olympdev.worker.job import JobStatus
from olympdev.worker.roles import REQUIRED_ROLES, ProgramRole
from olympdev.worker.jobs.simple_run import (
    ModelSolutionRunJob,
    SimpleRunJobResult,
    SolutionRunJob,
)
from olympdev.worker.jobs.tool_run import (
    CheckerJob,
    GeneratorJob,
    ToolRunJobResult,
    ValidatorJob,
    checker_verdict,
)
from olympdev.worker.pipeline import (
    AbstractPipeline,
    NextJobStatus,
    PipelineContext,
    ScheduledJob,
)
from olympdev.worker.verdict import Verdict

def testing_labels(
    *,
    problem_name: str | None = None,
    binary: str | None = None,
    test: str | None = None,
) -> dict[str, str]:
    labels: dict[str, str] = {}
    if problem_name:
        labels["Problem"] = problem_name
    if test:
        labels["Test"] = Path(test).name
    if binary:
        labels["Binary"] = binary
    return labels


def solution_verdict(meta: IsolateMeta) -> Verdict | None:
    if meta.status == "TO":
        return Verdict.TL
    if meta.cg_oom_killed:
        return Verdict.ML
    if not meta.is_successful_termination():
        return Verdict.RE
    return None


def _tool_ok(meta: IsolateMeta) -> bool:
    return meta.is_successful_termination() and meta.exitcode == 0


class RunLimits(FrozenModel):
    time_limit_ms: int
    wall_time_ms: int
    memory_limit_mb: int
    aspace_limit_kb: int | None
    file_size_limit_kb: int

    @classmethod
    def from_configuration(cls, configuration: ProblemConfiguration) -> Self:
        return cls(
            time_limit_ms=configuration.time_limit_ms,
            wall_time_ms=configuration.get_wall_limit(),
            memory_limit_mb=configuration.memory_limit_mb,
            aspace_limit_kb=configuration.aspace_limit_kb,
            file_size_limit_kb=configuration.get_file_size_limit(),
        )

    @model_validator(mode="after")
    def _check_limits(self) -> Self:
        IsolateLimits(
            time_limit_ms=self.time_limit_ms,
            wall_time_ms=self.wall_time_ms,
            memory_limit_mb=self.memory_limit_mb,
            aspace_limit_kb=self.aspace_limit_kb,
            file_size_limit_kb=self.file_size_limit_kb,
        )
        return self


class TestingContext(FrozenModel):
    limits: RunLimits
    problem_name: str | None = None
    #: Display name of each role's binary, for job labels. Absent roles simply
    #: have no entry.
    binary_names: dict[ProgramRole, str] = {}

    def labels(
        self,
        role: ProgramRole | None = None,
        test: str | None = None,
    ) -> dict[str, str]:
        return testing_labels(
            problem_name=self.problem_name,
            binary=None if role is None else self.binary_names.get(role),
            test=test,
        )

    def testing_kwargs(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in TestingContext.model_fields}


class ProblemTestingCase(FrozenModel):
    name: str
    input_digest: str | None = None
    output_digest: str | None = None
    gen_argv: list[str] | None = None
    #: Extra argv for the validator, e.g. ``["--group", "1"]``. Empty, not
    #: optional - there is no "exactly one of" invariant like gen_argv's.
    valid_argv: list[str] = []

    @model_validator(mode="after")
    def _input_or_gen(self) -> Self:
        if (self.input_digest is None) == (self.gen_argv is None):
            raise ValueError("exactly one of input_digest or gen_argv")
        return self


class ProblemTestArguments(TestingContext, StorageModel):
    name: str
    input_digest: str | None
    output_digest: str | None
    #: Compiled binary of each role that this problem uses.
    programs: dict[ProgramRole, str]
    gen_argv: list[str] | None = None
    valid_argv: list[str] = []
    index: int = 0

    def program(self, role: ProgramRole) -> str | None:
        return self.programs.get(role)

    @model_validator(mode="after")
    def _input_or_gen(self) -> Self:
        if (self.input_digest is None) == (self.gen_argv is None):
            raise ValueError("exactly one of input_digest or gen_argv")
        return self

    @model_validator(mode="after")
    def _check_required_roles(self) -> Self:
        missing = REQUIRED_ROLES - set(self.programs)
        if missing:
            raise ValueError(
                "missing programs: "
                + ", ".join(sorted(role.value for role in missing))
            )
        return self

    @classmethod
    def from_parent(
        cls,
        parent: TestingContext,
        case: ProblemTestingCase,
        *,
        programs: dict[ProgramRole, str],
        index: int,
    ) -> Self:
        return cls(
            name=case.name,
            input_digest=case.input_digest,
            output_digest=case.output_digest,
            programs=programs,
            gen_argv=case.gen_argv,
            valid_argv=case.valid_argv,
            index=index,
            **parent.testing_kwargs(),
        )

    def validate_digests(self, storage: AbstractStorage) -> Self:
        for digest in (self.input_digest, self.output_digest):
            if digest is not None:
                storage.validate_digest(digest)
        for digest in self.programs.values():
            storage.validate_digest(digest)
        return self


class ProblemTestResult(StorageModel):
    name: str
    verdict: Verdict
    run_digest: str | None
    check_digest: str | None = None
    #: The bytes this test fed the solution, whether they came from a file or
    #: from the generator. Reporting it is what lets an author read a test
    #: back with ``olympdev show`` instead of regenerating it by hand.
    input_digest: str | None = None


class ProblemTestPipeline(AbstractPipeline):
    """Run a solution on one test, then the checker if the solution produced output."""

    pipeline_type = "problem_test"

    #: Step names in this pipeline's step table.
    GEN = "gen"
    VALID = "valid"
    RUN = "run"
    MODEL = "model"
    CHECK = "check"

    def __init__(self, pipeline_id: str = "problem-test"):
        super().__init__(pipeline_id)
        # Cache only: re-derived from storage after a restore.
        self._arguments: ProblemTestArguments | None = None

    @classmethod
    def make_input_digest(
        cls,
        storage: AbstractStorage,
        arguments: ProblemTestArguments,
    ) -> str:
        return arguments.validate_digests(storage).upload(storage)

    def _parse_arguments(self, context: PipelineContext) -> ProblemTestArguments:
        if self._arguments is None:
            self._arguments = ProblemTestArguments.require_from_storage(
                context.storage,
                context.input_digest,
                missing="missing pipeline input",
            )
        return self._arguments

    def get_result(self, storage: AbstractStorage) -> ProblemTestResult | None:
        digest = self.get_output_digest()
        if digest is None:
            return None
        return ProblemTestResult.from_storage(storage, digest)

    def generated_input_digest(self, storage: AbstractStorage) -> str | None:
        """The generator's stdout digest (the actual test input), when this
        test's input came from a generator rather than a static file - see
        ``StressPipeline``, which needs the input that produced a mismatch."""
        step = self.step(self.GEN)
        if step is None:
            return None
        digest = step.job.get_output_digest()
        if digest is None:
            return None
        result = ToolRunJobResult.from_storage(storage, digest)
        return None if result is None else result.stdout_digest

    def model_run_digest(self) -> str | None:
        """The MODEL step's own job output digest, when one was run - see
        ``StressPipeline``, which uses this test's MODEL role as the
        reference solution."""
        step = self.step(self.MODEL)
        return None if step is None else step.job.get_output_digest()

    def run_not_started(self) -> bool:
        return not self.has_step(self.RUN)

    def is_busy(self) -> bool:
        return self.any_step_in_flight()

    def has_participant_answer(self, storage: AbstractStorage) -> bool:
        run = self.step(self.RUN)
        if (
            run is None
            or self.has_step(self.CHECK)
            or self.step_in_flight(self.RUN)
            or self.step_in_flight(self.MODEL)
        ):
            return False
        if run.job.get_status() is not JobStatus.COMPLETED:
            return False
        result = self._load_run(storage)
        return result is not None and solution_verdict(result.meta) is None

    def _next_job(self, context: PipelineContext) -> ScheduledJob | NextJobStatus:
        args = self._parse_arguments(context)
        storage = context.storage

        input_digest = self._ensure_input(storage, args)
        if not isinstance(input_digest, str):
            return input_digest

        valid_step = self._ensure_validated(storage, args, input_digest)
        if valid_step is not None:
            return valid_step

        if not self.has_step(self.RUN):
            return self._issue_run(storage, args, input_digest)
        if self.step_in_flight(self.RUN):
            return NextJobStatus.WAITING

        run_result, run_digest = self._take_run(storage, args)
        if run_result is None:
            return NextJobStatus.FINISHED

        verdict = solution_verdict(run_result.meta)
        if verdict is not None:
            return self._finish(storage, args, verdict, run_digest, None)

        answer_digest = self._answer_digest(storage, args, run_digest)
        if answer_digest is None:
            if self.is_finished():
                return NextJobStatus.FINISHED
            if self.step_in_flight(self.MODEL):
                return NextJobStatus.WAITING
            return self._issue_model(storage, args, input_digest)

        if not self.has_step(self.CHECK):
            return self._issue_check(
                storage, args, input_digest, run_result.stdout_digest, answer_digest
            )
        if self.step_in_flight(self.CHECK):
            return NextJobStatus.WAITING

        check_verdict, check_digest = self._take_check(storage, args)
        if check_verdict is None:
            return NextJobStatus.FINISHED
        return self._finish(storage, args, check_verdict, run_digest, check_digest)

    def _issue_run(
        self,
        storage: AbstractStorage,
        args: ProblemTestArguments,
        input_digest: str,
    ) -> ScheduledJob | NextJobStatus:
        if not storage.exists(input_digest):
            self.fail(f"missing test input {args.name}")
            return NextJobStatus.FINISHED
        return self.issue(
            self.RUN,
            SolutionRunJob,
            SolutionRunJob.make_input_digest(
                storage,
                program_digest=args.programs[ProgramRole.SOLUTION],
                input_digest=input_digest,
                **args.limits.to_dict(),
            ),
            job_id=f"run-{args.index}",
            labels=args.labels(ProgramRole.SOLUTION, args.name),
        )

    def _issue_check(
        self,
        storage: AbstractStorage,
        args: ProblemTestArguments,
        input_digest: str,
        stdout_digest: str,
        answer_digest: str,
    ) -> ScheduledJob:
        return self.issue(
            self.CHECK,
            CheckerJob,
            CheckerJob.make_input_digest(
                storage,
                program_digest=args.programs[ProgramRole.CHECKER],
                file_digests=[
                    (input_digest, "input.txt"),
                    (stdout_digest, "output.txt"),
                    (answer_digest, "answer.txt"),
                ],
                command=["input.txt", "output.txt", "answer.txt"],
                file_size_limit_kb=16 * 1024,
            ),
            job_id=f"check-{args.index}",
            labels=args.labels(ProgramRole.CHECKER, args.name),
        )

    def _issue_model(
        self,
        storage: AbstractStorage,
        args: ProblemTestArguments,
        input_digest: str,
    ) -> ScheduledJob | NextJobStatus:
        model_digest = args.program(ProgramRole.MODEL)
        if model_digest is None:
            self.fail(f"no answer and no model solution for {args.name}")
            return NextJobStatus.FINISHED
        return self.issue(
            self.MODEL,
            ModelSolutionRunJob,
            ModelSolutionRunJob.make_input_digest(
                storage,
                program_digest=model_digest,
                input_digest=input_digest,
                **args.limits.to_dict(),
            ),
            job_id=f"model-{args.index}",
            labels=args.labels(ProgramRole.MODEL, args.name),
        )

    def _issue_gen(
        self, storage: AbstractStorage, args: ProblemTestArguments
    ) -> ScheduledJob | NextJobStatus:
        generator_digest = args.program(ProgramRole.GENERATOR)
        if generator_digest is None or args.gen_argv is None:
            self.fail(f"no input and no generator for {args.name}")
            return NextJobStatus.FINISHED
        return self.issue(
            self.GEN,
            GeneratorJob,
            GeneratorJob.make_input_digest(
                storage,
                program_digest=generator_digest,
                command=args.gen_argv,
                file_size_limit_kb=args.limits.file_size_limit_kb,
            ),
            job_id=f"gen-{args.index}",
            labels=args.labels(ProgramRole.GENERATOR, args.name),
        )

    def _issue_valid(
        self,
        storage: AbstractStorage,
        args: ProblemTestArguments,
        input_digest: str,
    ) -> ScheduledJob:
        validator_digest = args.program(ProgramRole.VALIDATOR)
        assert validator_digest is not None
        return self.issue(
            self.VALID,
            ValidatorJob,
            ValidatorJob.make_input_digest(
                storage,
                program_digest=validator_digest,
                stdin_digest=input_digest,
                command=args.valid_argv,
            ),
            job_id=f"valid-{args.index}",
            labels=args.labels(ProgramRole.VALIDATOR, args.name),
        )

    def _ensure_input(
        self, storage: AbstractStorage, args: ProblemTestArguments
    ) -> str | ScheduledJob | NextJobStatus:
        if args.input_digest is not None:
            return args.input_digest
        if not self.has_step(self.GEN):
            return self._issue_gen(storage, args)
        if self.step_in_flight(self.GEN):
            return NextJobStatus.WAITING
        gen_result, _digest = self.step_result(
            self.GEN,
            ToolRunJobResult,
            storage,
            missing=f"missing generator result for {args.name}",
        )
        if gen_result is None:
            return NextJobStatus.FINISHED
        if not _tool_ok(gen_result.meta):
            return self._finish(storage, args, Verdict.CF, None, None)
        # Derived from the step table, so a restored pipeline finds it again.
        return gen_result.stdout_digest

    def _ensure_validated(
        self,
        storage: AbstractStorage,
        args: ProblemTestArguments,
        input_digest: str,
    ) -> ScheduledJob | NextJobStatus | None:
        if args.program(ProgramRole.VALIDATOR) is None or self.has_step(self.RUN):
            return None
        if not self.has_step(self.VALID):
            return self._issue_valid(storage, args, input_digest)
        if self.step_in_flight(self.VALID):
            return NextJobStatus.WAITING
        valid_result, _digest = self.step_result(
            self.VALID,
            ToolRunJobResult,
            storage,
            missing=f"missing validator result for {args.name}",
        )
        if valid_result is None:
            return NextJobStatus.FINISHED
        if not _tool_ok(valid_result.meta):
            return self._finish(storage, args, Verdict.CF, None, None)
        return None

    def _answer_digest(
        self,
        storage: AbstractStorage,
        args: ProblemTestArguments,
        run_digest: str,
    ) -> str | None:
        if args.output_digest is not None:
            return args.output_digest
        if not self.has_step(self.MODEL) or self.step_in_flight(self.MODEL):
            return None
        model_result, _model_digest = self.step_result(
            self.MODEL,
            SimpleRunJobResult,
            storage,
            missing=f"missing model result for {args.name}",
        )
        if model_result is None:
            return None
        if solution_verdict(model_result.meta) is not None:
            self._finish(storage, args, Verdict.FL, run_digest, None)
            return None
        return model_result.stdout_digest

    def _load_run(self, storage: AbstractStorage) -> SimpleRunJobResult | None:
        run = self.step(self.RUN)
        assert run is not None
        digest = run.job.get_output_digest()
        if digest is None:
            return None
        return SimpleRunJobResult.from_storage(storage, digest)

    def _take_run(
        self, storage: AbstractStorage, args: ProblemTestArguments
    ) -> tuple[SimpleRunJobResult, str] | tuple[None, None]:
        return self.step_result(
            self.RUN,
            SimpleRunJobResult,
            storage,
            missing=f"missing run result for {args.name}",
        )

    def _take_check(
        self, storage: AbstractStorage, args: ProblemTestArguments
    ) -> tuple[Verdict, str] | tuple[None, None]:
        check_result, check_digest = self.step_result(
            self.CHECK,
            ToolRunJobResult,
            storage,
            missing=f"missing checker result for {args.name}",
        )
        if check_result is None:
            return None, None
        return checker_verdict(check_result.meta), check_digest

    def _finish(
        self,
        storage: AbstractStorage,
        args: ProblemTestArguments,
        verdict: Verdict,
        run_digest: str | None,
        check_digest: str | None,
    ) -> NextJobStatus:
        self.complete(
            ProblemTestResult(
                name=args.name,
                verdict=verdict,
                run_digest=run_digest,
                check_digest=check_digest,
                input_digest=self._resolved_input_digest(storage, args),
            ).upload(storage)
        )
        return NextJobStatus.FINISHED

    def _resolved_input_digest(
        self, storage: AbstractStorage, args: ProblemTestArguments
    ) -> str | None:
        """This test's input, from its file or from the generator that made it.

        Read back out of the step table rather than remembered, so a restored
        pipeline resolves it the same way ``_ensure_input`` does.
        """
        if args.input_digest is not None:
            return args.input_digest
        return self.generated_input_digest(storage)
