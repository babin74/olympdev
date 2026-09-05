from pathlib import Path
from typing import Any, Self, TypeVar

from pydantic import model_validator

from olympdev.isolate import IsolateLimits, IsolateMeta
from olympdev.problem.types import ProblemConfiguration
from olympdev.schema import FrozenModel, StorageModel
from olympdev.storage import AbstractStorage
from olympdev.worker.job import JobStatus
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

_IN_FLIGHT = (JobStatus.PENDING, JobStatus.RUNNING)
_TResult = TypeVar("_TResult", bound=StorageModel)


def _in_flight(job: ScheduledJob | None) -> bool:
    return job is not None and job.job.get_status() in _IN_FLIGHT


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
            time_limit_ms=configuration.time_limit,
            wall_time_ms=configuration.get_wall_limit(),
            memory_limit_mb=configuration.memory_limit,
            aspace_limit_kb=configuration.aspace_limit,
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
    solution_binary_name: str | None = None
    checker_binary_name: str | None = None
    model_binary_name: str | None = None
    generator_binary_name: str | None = None
    validator_binary_name: str | None = None

    def labels(
        self, *, binary: str | None = None, test: str | None = None
    ) -> dict[str, str]:
        return testing_labels(
            problem_name=self.problem_name, binary=binary, test=test
        )

    def solution_labels(self, test: str | None = None) -> dict[str, str]:
        return self.labels(binary=self.solution_binary_name, test=test)

    def checker_labels(self, test: str | None = None) -> dict[str, str]:
        return self.labels(binary=self.checker_binary_name, test=test)

    def model_labels(self, test: str | None = None) -> dict[str, str]:
        return self.labels(binary=self.model_binary_name, test=test)

    def generator_labels(self, test: str | None = None) -> dict[str, str]:
        return self.labels(binary=self.generator_binary_name, test=test)

    def validator_labels(self, test: str | None = None) -> dict[str, str]:
        return self.labels(binary=self.validator_binary_name, test=test)

    def testing_kwargs(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in TestingContext.model_fields}


class ProblemTestingCase(FrozenModel):
    name: str
    input_digest: str | None = None
    output_digest: str | None = None
    gen_argv: list[str] | None = None

    @model_validator(mode="after")
    def _input_or_gen(self) -> Self:
        if (self.input_digest is None) == (self.gen_argv is None):
            raise ValueError("exactly one of input_digest or gen_argv")
        return self


class ProblemTestArguments(TestingContext, StorageModel):
    name: str
    input_digest: str | None
    output_digest: str | None
    program_digest: str
    checker_program_digest: str
    model_program_digest: str | None = None
    generator_program_digest: str | None = None
    validator_program_digest: str | None = None
    gen_argv: list[str] | None = None
    index: int = 0

    @model_validator(mode="after")
    def _input_or_gen(self) -> Self:
        if (self.input_digest is None) == (self.gen_argv is None):
            raise ValueError("exactly one of input_digest or gen_argv")
        return self

    @classmethod
    def from_parent(
        cls,
        parent: TestingContext,
        case: ProblemTestingCase,
        *,
        program_digest: str,
        checker_program_digest: str,
        model_program_digest: str | None,
        generator_program_digest: str | None,
        validator_program_digest: str | None,
        index: int,
    ) -> Self:
        return cls(
            name=case.name,
            input_digest=case.input_digest,
            output_digest=case.output_digest,
            program_digest=program_digest,
            checker_program_digest=checker_program_digest,
            model_program_digest=model_program_digest,
            generator_program_digest=generator_program_digest,
            validator_program_digest=validator_program_digest,
            gen_argv=case.gen_argv,
            index=index,
            **parent.testing_kwargs(),
        )

    def validate_digests(self, storage: AbstractStorage) -> Self:
        if self.input_digest is not None:
            storage.validate_digest(self.input_digest)
        storage.validate_digest(self.program_digest)
        storage.validate_digest(self.checker_program_digest)
        if self.output_digest is not None:
            storage.validate_digest(self.output_digest)
        if self.model_program_digest is not None:
            storage.validate_digest(self.model_program_digest)
        if self.generator_program_digest is not None:
            storage.validate_digest(self.generator_program_digest)
        if self.validator_program_digest is not None:
            storage.validate_digest(self.validator_program_digest)
        return self


class ProblemTestResult(StorageModel):
    name: str
    verdict: Verdict
    run_digest: str | None
    check_digest: str | None = None


class ProblemTestPipeline(AbstractPipeline):
    """Run a solution on one test, then the checker if the solution produced output."""

    def __init__(self, pipeline_id: str = "problem-test"):
        super().__init__(pipeline_id)
        self._arguments: ProblemTestArguments | None = None
        self._gen: ScheduledJob | None = None
        self._valid: ScheduledJob | None = None
        self._run: ScheduledJob | None = None
        self._model: ScheduledJob | None = None
        self._check: ScheduledJob | None = None
        self._generated_input: str | None = None

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

    def run_not_started(self) -> bool:
        return self._run is None

    def is_busy(self) -> bool:
        return (
            _in_flight(self._gen)
            or _in_flight(self._valid)
            or _in_flight(self._run)
            or _in_flight(self._model)
            or _in_flight(self._check)
        )

    def has_participant_answer(self, storage: AbstractStorage) -> bool:
        if (
            self._run is None
            or self._check is not None
            or _in_flight(self._run)
            or _in_flight(self._model)
        ):
            return False
        if self._run.job.get_status() is not JobStatus.COMPLETED:
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

        if self._run is None:
            return self._issue_run(storage, args, input_digest)
        if _in_flight(self._run):
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
            if _in_flight(self._model):
                return NextJobStatus.WAITING
            return self._issue_model(storage, args, input_digest)

        if self._check is None:
            return self._issue_check(
                storage, args, input_digest, run_result.stdout_digest, answer_digest
            )
        if _in_flight(self._check):
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
        scheduled = ScheduledJob(
            job=SolutionRunJob(
                f"run-{args.index}",
                labels=args.solution_labels(args.name),
            ),
            input_digest=SolutionRunJob.make_input_digest(
                storage,
                program_digest=args.program_digest,
                input_digest=input_digest,
                **args.limits.to_dict(),
            ),
        )
        self._run = scheduled
        return scheduled

    def _issue_check(
        self,
        storage: AbstractStorage,
        args: ProblemTestArguments,
        input_digest: str,
        stdout_digest: str,
        answer_digest: str,
    ) -> ScheduledJob:
        scheduled = ScheduledJob(
            job=CheckerJob(
                f"check-{args.index}",
                labels=args.checker_labels(args.name),
            ),
            input_digest=CheckerJob.make_input_digest(
                storage,
                program_digest=args.checker_program_digest,
                file_digests=[
                    (input_digest, "input.txt"),
                    (stdout_digest, "output.txt"),
                    (answer_digest, "answer.txt"),
                ],
                command=["input.txt", "output.txt", "answer.txt"],
                file_size_limit_kb=16 * 1024,
            ),
        )
        self._check = scheduled
        return scheduled

    def _issue_model(
        self,
        storage: AbstractStorage,
        args: ProblemTestArguments,
        input_digest: str,
    ) -> ScheduledJob | NextJobStatus:
        if args.model_program_digest is None:
            self.fail(f"no answer and no model solution for {args.name}")
            return NextJobStatus.FINISHED
        scheduled = ScheduledJob(
            job=ModelSolutionRunJob(
                f"model-{args.index}",
                labels=args.model_labels(args.name),
            ),
            input_digest=ModelSolutionRunJob.make_input_digest(
                storage,
                program_digest=args.model_program_digest,
                input_digest=input_digest,
                **args.limits.to_dict(),
            ),
        )
        self._model = scheduled
        return scheduled

    def _issue_gen(
        self, storage: AbstractStorage, args: ProblemTestArguments
    ) -> ScheduledJob | NextJobStatus:
        if args.generator_program_digest is None or args.gen_argv is None:
            self.fail(f"no input and no generator for {args.name}")
            return NextJobStatus.FINISHED
        scheduled = ScheduledJob(
            job=GeneratorJob(
                f"gen-{args.index}",
                labels=args.generator_labels(args.name),
            ),
            input_digest=GeneratorJob.make_input_digest(
                storage,
                program_digest=args.generator_program_digest,
                command=args.gen_argv,
                file_size_limit_kb=args.limits.file_size_limit_kb,
            ),
        )
        self._gen = scheduled
        return scheduled

    def _issue_valid(
        self,
        storage: AbstractStorage,
        args: ProblemTestArguments,
        input_digest: str,
    ) -> ScheduledJob:
        assert args.validator_program_digest is not None
        scheduled = ScheduledJob(
            job=ValidatorJob(
                f"valid-{args.index}",
                labels=args.validator_labels(args.name),
            ),
            input_digest=ValidatorJob.make_input_digest(
                storage,
                program_digest=args.validator_program_digest,
                stdin_digest=input_digest,
            ),
        )
        self._valid = scheduled
        return scheduled

    def _ensure_input(
        self, storage: AbstractStorage, args: ProblemTestArguments
    ) -> str | ScheduledJob | NextJobStatus:
        if args.input_digest is not None:
            return args.input_digest
        if self._generated_input is not None:
            return self._generated_input
        if self._gen is None:
            return self._issue_gen(storage, args)
        if _in_flight(self._gen):
            return NextJobStatus.WAITING
        gen_result, _digest = self._take_job_output(
            self._gen,
            storage,
            ToolRunJobResult,
            f"missing generator result for {args.name}",
        )
        if gen_result is None:
            return NextJobStatus.FINISHED
        if not _tool_ok(gen_result.meta):
            return self._finish(storage, args, Verdict.CF, None, None)
        self._generated_input = gen_result.stdout_digest
        return self._generated_input

    def _ensure_validated(
        self,
        storage: AbstractStorage,
        args: ProblemTestArguments,
        input_digest: str,
    ) -> ScheduledJob | NextJobStatus | None:
        if args.validator_program_digest is None or self._run is not None:
            return None
        if self._valid is None:
            return self._issue_valid(storage, args, input_digest)
        if _in_flight(self._valid):
            return NextJobStatus.WAITING
        valid_result, _digest = self._take_job_output(
            self._valid,
            storage,
            ToolRunJobResult,
            f"missing validator result for {args.name}",
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
        if self._model is None or _in_flight(self._model):
            return None
        model_result, _model_digest = self._take_job_output(
            self._model,
            storage,
            SimpleRunJobResult,
            f"missing model result for {args.name}",
        )
        if model_result is None:
            return None
        if solution_verdict(model_result.meta) is not None:
            self._finish(storage, args, Verdict.FL, run_digest, None)
            return None
        return model_result.stdout_digest

    def _load_run(self, storage: AbstractStorage) -> SimpleRunJobResult | None:
        assert self._run is not None
        digest = self._run.job.get_output_digest()
        if digest is None:
            return None
        return SimpleRunJobResult.from_storage(storage, digest)

    def _take_job_output(
        self,
        scheduled: ScheduledJob,
        storage: AbstractStorage,
        result_type: type[_TResult],
        missing: str,
    ) -> tuple[_TResult, str] | tuple[None, None]:
        if scheduled.job.get_status() is JobStatus.FAILED:
            self.fail(scheduled.job.get_failure_reason())
            return None, None
        digest = scheduled.job.get_output_digest()
        result = (
            result_type.from_storage(
                storage, digest) if digest is not None else None
        )
        if result is None:
            self.fail(missing)
            return None, None
        assert digest is not None
        return result, digest

    def _take_run(
        self, storage: AbstractStorage, args: ProblemTestArguments
    ) -> tuple[SimpleRunJobResult, str] | tuple[None, None]:
        assert self._run is not None
        return self._take_job_output(
            self._run,
            storage,
            SimpleRunJobResult,
            f"missing run result for {args.name}",
        )

    def _take_check(
        self, storage: AbstractStorage, args: ProblemTestArguments
    ) -> tuple[Verdict, str] | tuple[None, None]:
        assert self._check is not None
        check_result, check_digest = self._take_job_output(
            self._check,
            storage,
            ToolRunJobResult,
            f"missing checker result for {args.name}",
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
            ).upload(storage)
        )
        return NextJobStatus.FINISHED
