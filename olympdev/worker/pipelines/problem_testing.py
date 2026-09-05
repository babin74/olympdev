from pathlib import Path
from typing import Callable, Self

from pydantic import model_validator

from olympdev.problem.types import (
    ProblemConfiguration,
    ProblemTest,
    parse_gen_argv,
    resolve_problem_file,
)
from olympdev.schema import StorageModel
from olympdev.settings import get_settings
from olympdev.storage import AbstractStorage
from olympdev.worker.job import JobStatus
from olympdev.worker.jobs.compile import (
    CheckerCompileJob,
    CompileJob,
    CompileJobResult,
    GeneratorCompileJob,
    ModelSolutionCompileJob,
    SolutionCompileJob,
    ValidatorCompileJob,
)
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
from olympdev.worker.verdict import Verdict

_IN_FLIGHT = (JobStatus.PENDING, JobStatus.RUNNING)
_SOURCE_SUFFIXES = {".c", ".cc", ".cpp", ".cxx"}

ProblemTestingTestResult = ProblemTestResult


class ProblemTestingArguments(TestingContext, StorageModel):
    compile_input_digest: str
    checker_compile_input_digest: str
    model_compile_input_digest: str | None = None
    generator_compile_input_digest: str | None = None
    validator_compile_input_digest: str | None = None
    tests: list[ProblemTestingCase]

    @model_validator(mode="after")
    def _check_tests(self) -> Self:
        if len(self.tests) > get_settings().limits.max_tests_per_problem:
            raise ValueError(f"too many tests: {len(self.tests)}")
        if (
            any(test.output_digest is None for test in self.tests)
            and self.model_compile_input_digest is None
        ):
            raise ValueError(
                "model solution is required when tests have no output file"
            )
        if (
            any(test.gen_argv is not None for test in self.tests)
            and self.generator_compile_input_digest is None
        ):
            raise ValueError(
                "generator is required when tests use gen_cmd"
            )
        return self


class ProblemTestingResult(StorageModel):
    verdict: Verdict
    compile_digest: str | None
    checker_compile_digest: str | None
    tests: list[ProblemTestResult]
    model_compile_digest: str | None = None
    generator_compile_digest: str | None = None
    validator_compile_digest: str | None = None


def _source_binary_name(
    sources: list[tuple[Path | bytes, str | Path]],
) -> str | None:
    """Display name of a binary: stem of the first C/C++ source path."""
    for source, sandbox_path in sources:
        path = source if isinstance(source, Path) else Path(sandbox_path)
        if path.suffix.lower() in _SOURCE_SUFFIXES:
            return path.stem
    return None


def _optional_compile_digest(
    storage: AbstractStorage,
    sources: list[tuple[Path | bytes, str | Path]] | None,
    command: list[str | int] | None,
    *,
    name: str,
    job_cls: type[CompileJob],
) -> str | None:
    if (sources is None) != (command is None):
        raise ValueError(f"{name}_sources and {name}_command must be provided together")
    if sources is None:
        return None
    assert command is not None
    return job_cls.make_input_digest(storage, sources=sources, command=command)


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


def _in_flight(job: ScheduledJob | None) -> bool:
    return job is not None and job.job.get_status() in _IN_FLIGHT


def _compile_result(
    storage: AbstractStorage, job: ScheduledJob
) -> CompileJobResult | None:
    compile_job = job.job
    assert isinstance(compile_job, CompileJob)
    return compile_job.get_result(storage)


class ProblemTestingPipeline(AbstractPipeline):
    def __init__(self, pipeline_id: str = "problem-testing"):
        super().__init__(pipeline_id)
        self._arguments: ProblemTestingArguments | None = None
        self._solution_compile: ScheduledJob | None = None
        self._checker_compile: ScheduledJob | None = None
        self._model_compile: ScheduledJob | None = None
        self._generator_compile: ScheduledJob | None = None
        self._validator_compile: ScheduledJob | None = None
        self._program_digest: str | None = None
        self._checker_program: str | None = None
        self._model_program: str | None = None
        self._generator_program: str | None = None
        self._validator_program: str | None = None
        self._tests: list[ScheduledPipeline] | None = None

    @classmethod
    def make_input_digest(
        cls,
        storage: AbstractStorage,
        *,
        sources: list[tuple[Path | bytes, str | Path]],
        command: list[str | int],
        checker_sources: list[tuple[Path | bytes, str | Path]],
        checker_command: list[str | int],
        configuration: ProblemConfiguration,
        problem_dir: Path,
        problem_name: str | None = None,
        model_sources: list[tuple[Path | bytes, str | Path]] | None = None,
        model_command: list[str | int] | None = None,
        generator_sources: list[tuple[Path | bytes, str | Path]] | None = None,
        generator_command: list[str | int] | None = None,
        validator_sources: list[tuple[Path | bytes, str | Path]] | None = None,
        validator_command: list[str | int] | None = None,
    ) -> str:
        tests = [
            _store_test(storage, problem_dir, test)
            for test in [*configuration.samples, *configuration.tests]
        ]
        return ProblemTestingArguments(
            compile_input_digest=SolutionCompileJob.make_input_digest(
                storage, sources=sources, command=command
            ),
            checker_compile_input_digest=CheckerCompileJob.make_input_digest(
                storage, sources=checker_sources, command=checker_command
            ),
            model_compile_input_digest=_optional_compile_digest(
                storage,
                model_sources,
                model_command,
                name="model",
                job_cls=ModelSolutionCompileJob,
            ),
            generator_compile_input_digest=_optional_compile_digest(
                storage,
                generator_sources,
                generator_command,
                name="generator",
                job_cls=GeneratorCompileJob,
            ),
            validator_compile_input_digest=_optional_compile_digest(
                storage,
                validator_sources,
                validator_command,
                name="validator",
                job_cls=ValidatorCompileJob,
            ),
            limits=RunLimits.from_configuration(configuration),
            problem_name=problem_name,
            solution_binary_name=_source_binary_name(sources),
            checker_binary_name=_source_binary_name(checker_sources),
            model_binary_name=_source_binary_name(model_sources or []),
            generator_binary_name=_source_binary_name(generator_sources or []),
            validator_binary_name=_source_binary_name(validator_sources or []),
            tests=tests,
        ).upload(storage)

    def _parse_arguments(self, context: PipelineContext) -> ProblemTestingArguments:
        if self._arguments is None:
            args = ProblemTestingArguments.require_from_storage(
                context.storage,
                context.input_digest,
                missing="missing pipeline input",
            )
            context.storage.validate_digest(args.compile_input_digest)
            context.storage.validate_digest(args.checker_compile_input_digest)
            if args.model_compile_input_digest is not None:
                context.storage.validate_digest(args.model_compile_input_digest)
            if args.generator_compile_input_digest is not None:
                context.storage.validate_digest(args.generator_compile_input_digest)
            if args.validator_compile_input_digest is not None:
                context.storage.validate_digest(args.validator_compile_input_digest)
            for test in args.tests:
                if test.input_digest is not None:
                    context.storage.validate_digest(test.input_digest)
                if test.output_digest is not None:
                    context.storage.validate_digest(test.output_digest)
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

        nxt = self._pull_first(
            tests, context, lambda child: child.has_participant_answer(storage))
        if nxt is not None:
            return nxt

        nxt = self._pull_first(
            tests, context, lambda child: child.run_not_started())
        if nxt is not None:
            return nxt

        if any(self._child(s).is_busy() for s in tests):
            return NextJobStatus.WAITING

        nxt = self._pull_first(
            tests, context, lambda child: not child.is_finished()
        )
        if nxt is not None:
            return nxt

        return self._complete_overall(storage)

    def _step_compiles(
        self, context: PipelineContext, args: ProblemTestingArguments
    ) -> ScheduledJob | NextJobStatus | None:
        if self._program_digest is not None:
            return None
        if self._solution_compile is None:
            self._solution_compile = self._issue_compile(
                "compile-solution",
                args.compile_input_digest,
                args.solution_labels(),
                SolutionCompileJob,
            )
            return self._solution_compile
        if self._checker_compile is None:
            self._checker_compile = self._issue_compile(
                "compile-checker",
                args.checker_compile_input_digest,
                args.checker_labels(),
                CheckerCompileJob,
            )
            return self._checker_compile
        if (
            args.model_compile_input_digest is not None
            and self._model_compile is None
        ):
            self._model_compile = self._issue_compile(
                "compile-model",
                args.model_compile_input_digest,
                args.model_labels(),
                ModelSolutionCompileJob,
            )
            return self._model_compile
        if (
            args.generator_compile_input_digest is not None
            and self._generator_compile is None
        ):
            self._generator_compile = self._issue_compile(
                "compile-generator",
                args.generator_compile_input_digest,
                args.generator_labels(),
                GeneratorCompileJob,
            )
            return self._generator_compile
        if (
            args.validator_compile_input_digest is not None
            and self._validator_compile is None
        ):
            self._validator_compile = self._issue_compile(
                "compile-validator",
                args.validator_compile_input_digest,
                args.validator_labels(),
                ValidatorCompileJob,
            )
            return self._validator_compile
        if (
            _in_flight(self._solution_compile)
            or _in_flight(self._checker_compile)
            or _in_flight(self._model_compile)
            or _in_flight(self._generator_compile)
            or _in_flight(self._validator_compile)
        ):
            return NextJobStatus.WAITING
        return self._finish_compiles(context.storage, args)

    def _issue_compile(
        self,
        job_id: str,
        input_digest: str,
        labels: dict[str, str],
        job_cls: type[CompileJob],
    ) -> ScheduledJob:
        return ScheduledJob(
            job=job_cls(job_id, labels=labels),
            input_digest=input_digest,
        )

    def _finish_compiles(
        self, storage: AbstractStorage, args: ProblemTestingArguments
    ) -> NextJobStatus | None:
        assert self._solution_compile is not None and self._checker_compile is not None
        solution_result = _compile_result(storage, self._solution_compile)
        checker_result = _compile_result(storage, self._checker_compile)
        model_result = self._optional_compile_result(storage, self._model_compile)
        generator_result = self._optional_compile_result(
            storage, self._generator_compile
        )
        validator_result = self._optional_compile_result(
            storage, self._validator_compile
        )

        if self._failed_compile(
            storage, self._solution_compile, solution_result, Verdict.CE
        ):
            return NextJobStatus.FINISHED
        if self._failed_compile(
            storage, self._checker_compile, checker_result, Verdict.CF
        ):
            return NextJobStatus.FINISHED
        if self._model_compile is not None and self._failed_compile(
            storage, self._model_compile, model_result, Verdict.FL
        ):
            return NextJobStatus.FINISHED
        if self._generator_compile is not None and self._failed_compile(
            storage, self._generator_compile, generator_result, Verdict.CF
        ):
            return NextJobStatus.FINISHED
        if self._validator_compile is not None and self._failed_compile(
            storage, self._validator_compile, validator_result, Verdict.CF
        ):
            return NextJobStatus.FINISHED

        if (
            solution_result is None
            or checker_result is None
            or solution_result.program_digest is None
            or checker_result.program_digest is None
        ):
            self.fail("Compilation produced no program")
            return NextJobStatus.FINISHED
        if args.model_compile_input_digest is not None and (
            model_result is None or model_result.program_digest is None
        ):
            self.fail("Model compilation produced no program")
            return NextJobStatus.FINISHED
        if args.generator_compile_input_digest is not None and (
            generator_result is None or generator_result.program_digest is None
        ):
            self.fail("Generator compilation produced no program")
            return NextJobStatus.FINISHED
        if args.validator_compile_input_digest is not None and (
            validator_result is None or validator_result.program_digest is None
        ):
            self.fail("Validator compilation produced no program")
            return NextJobStatus.FINISHED
        self._program_digest = solution_result.program_digest
        self._checker_program = checker_result.program_digest
        self._model_program = (
            None if model_result is None else model_result.program_digest
        )
        self._generator_program = (
            None if generator_result is None else generator_result.program_digest
        )
        self._validator_program = (
            None if validator_result is None else validator_result.program_digest
        )
        return None

    def _optional_compile_result(
        self, storage: AbstractStorage, job: ScheduledJob | None
    ) -> CompileJobResult | None:
        if job is None:
            return None
        return _compile_result(storage, job)

    def _compile_output(self, job: ScheduledJob | None) -> str | None:
        if job is None:
            return None
        return job.job.get_output_digest()

    def _failed_compile(
        self,
        storage: AbstractStorage,
        job: ScheduledJob,
        result: CompileJobResult | None,
        domain_verdict: Verdict,
    ) -> bool:
        if job.job.get_status() is not JobStatus.FAILED:
            return False
        if result is None or result.meta.is_sandbox_error():
            self.fail(job.job.get_failure_reason())
        else:
            self._complete_with(storage, domain_verdict, [])
        return True

    def _ensure_tests(
        self, storage: AbstractStorage, args: ProblemTestingArguments
    ) -> list[ScheduledPipeline]:
        if self._tests is None:
            assert self._program_digest is not None
            assert self._checker_program is not None
            self._tests = [
                ScheduledPipeline(
                    pipeline=ProblemTestPipeline(f"test-{index}"),
                    input_digest=ProblemTestPipeline.make_input_digest(
                        storage,
                        ProblemTestArguments.from_parent(
                            args,
                            case,
                            program_digest=self._program_digest,
                            checker_program_digest=self._checker_program,
                            model_program_digest=self._model_program,
                            generator_program_digest=self._generator_program,
                            validator_program_digest=self._validator_program,
                            index=index,
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

    def _complete_with(
        self,
        storage: AbstractStorage,
        verdict: Verdict,
        tests: list[ProblemTestResult],
    ) -> None:
        self.complete(
            ProblemTestingResult(
                verdict=verdict,
                compile_digest=self._compile_output(self._solution_compile),
                checker_compile_digest=self._compile_output(self._checker_compile),
                model_compile_digest=self._compile_output(self._model_compile),
                generator_compile_digest=self._compile_output(self._generator_compile),
                validator_compile_digest=self._compile_output(self._validator_compile),
                tests=tests,
            ).upload(storage)
        )

    def _complete_overall(self, storage: AbstractStorage) -> NextJobStatus:
        assert self._solution_compile is not None and self._checker_compile is not None
        assert self._tests is not None
        test_results: list[ProblemTestResult] = []
        overall = Verdict.OK
        for scheduled in self._tests:
            result = self._child(scheduled).get_result(storage)
            if result is None:
                self.fail(
                    f"missing test result for {scheduled.pipeline.pipeline_id}")
                return NextJobStatus.FINISHED
            test_results.append(result)
            if overall is Verdict.OK and result.verdict is not Verdict.OK:
                overall = result.verdict
        self._complete_with(storage, overall, test_results)
        return NextJobStatus.FINISHED
