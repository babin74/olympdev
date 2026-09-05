from typing import Self

from pydantic import model_validator

from olympdev.problem.types import (
    match_solution_tag,
    parse_solution_tag,
)
from olympdev.schema import FrozenModel, StorageModel
from olympdev.storage import AbstractStorage
from olympdev.worker.pipeline import (
    AbstractPipeline,
    NextJobStatus,
    PipelineContext,
    ScheduledJob,
    ScheduledPipeline,
)
from olympdev.worker.pipelines.problem_testing import (
    ProblemTestingPipeline,
    ProblemTestingResult,
)
from olympdev.worker.verdict import Verdict


class SolutionVerificationSpec(FrozenModel):
    tag: str
    testing_digest: str


class ProblemVerificationArguments(StorageModel):
    solutions: list[SolutionVerificationSpec]

    @model_validator(mode="after")
    def _check_solutions(self) -> Self:
        if not self.solutions:
            raise ValueError("no solutions to verify")
        for spec in self.solutions:
            if parse_solution_tag(spec.tag).ignored:
                raise ValueError(f"cannot verify ignored solution {spec.tag!r}")
        return self


class SolutionVerificationResult(FrozenModel):
    tag: str
    matched: bool
    reason: str | None = None
    #: Output digest of the child ``ProblemTestingPipeline``.
    testing_digest: str


class ProblemVerificationResult(StorageModel):
    matched: bool
    solutions: list[SolutionVerificationResult]


def _observed_verdicts(result: ProblemTestingResult) -> list[Verdict]:
    if result.tests:
        return [test.verdict for test in result.tests]
    return [result.verdict]


class ProblemVerificationPipeline(AbstractPipeline):
    """Run each listed solution, then check the verdicts against its tag."""

    pipeline_type = "problem_verification"

    def __init__(self, pipeline_id: str = "problem-verification"):
        super().__init__(pipeline_id)
        self._arguments: ProblemVerificationArguments | None = None

    @classmethod
    def make_input_digest(
        cls,
        storage: AbstractStorage,
        *,
        solutions: list[SolutionVerificationSpec],
    ) -> str:
        arguments = ProblemVerificationArguments(solutions=solutions)
        for spec in arguments.solutions:
            storage.validate_digest(spec.testing_digest)
        return arguments.upload(storage)

    def _parse_arguments(self, context: PipelineContext) -> ProblemVerificationArguments:
        if self._arguments is None:
            args = ProblemVerificationArguments.require_from_storage(
                context.storage,
                context.input_digest,
                missing="missing pipeline input",
            )
            for spec in args.solutions:
                context.storage.validate_digest(spec.testing_digest)
            self._arguments = args
        return self._arguments

    def get_result(self, storage: AbstractStorage) -> ProblemVerificationResult | None:
        digest = self.get_output_digest()
        if digest is None:
            return None
        return ProblemVerificationResult.from_storage(storage, digest)

    def _next_job(self, context: PipelineContext) -> ScheduledJob | NextJobStatus:
        args = self._parse_arguments(context)
        children = self._ensure_children(args)
        for scheduled in children:
            if scheduled.pipeline.is_finished():
                continue
            job = self.pull_child(scheduled, context)
            if self.is_finished():
                return NextJobStatus.FINISHED
            if job is not None:
                return job
        if any(not scheduled.pipeline.is_finished() for scheduled in children):
            return NextJobStatus.WAITING
        return self._complete_overall(context.storage, args)

    def _ensure_children(
        self, args: ProblemVerificationArguments
    ) -> list[ScheduledPipeline]:
        if not self._children:
            for index, spec in enumerate(args.solutions):
                self.add_child(
                    f"solution-{index}",
                    ProblemTestingPipeline(f"solution-{index}"),
                    spec.testing_digest,
                )
        return self.children()

    def _complete_overall(
        self,
        storage: AbstractStorage,
        args: ProblemVerificationArguments,
    ) -> NextJobStatus:
        results: list[SolutionVerificationResult] = []
        matched_all = True
        for spec, scheduled in zip(args.solutions, self.children(), strict=True):
            testing = scheduled.pipeline
            assert isinstance(testing, ProblemTestingPipeline)
            testing_result = testing.get_result(storage)
            output_digest = testing.get_output_digest()
            if testing_result is None or output_digest is None:
                self.fail(f"missing testing result for {spec.tag}")
                return NextJobStatus.FINISHED
            reason = match_solution_tag(
                parse_solution_tag(spec.tag),
                _observed_verdicts(testing_result),
            )
            results.append(
                SolutionVerificationResult(
                    tag=spec.tag,
                    matched=reason is None,
                    reason=reason,
                    testing_digest=output_digest,
                )
            )
            if reason is not None:
                matched_all = False
        self.complete(
            ProblemVerificationResult(
                matched=matched_all, solutions=results
            ).upload(storage)
        )
        return NextJobStatus.FINISHED
