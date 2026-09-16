"""Stress-testing a candidate solution against a reference, on growing seeds.

Per roi-rounds/discovered/rules.md §9 ("Стресс против перебора"): a problem
should not enter a tour without having its "obvious wrong idea" solutions
shown to actually break on some input, rather than only on the tests someone
happened to write by hand.

Reuses ``ProblemTestPipeline`` wholesale as the per-iteration child - the
same "generate (optional) -> validate (optional) -> run -> get answer (model,
since no static output) -> check" flow the ordinary testing pipeline already
runs, just fed the candidate as ``SOLUTION`` and the reference as ``MODEL``
with a growing ``--seed``-style generator argument (no VALIDATOR role: a
stress run does not aim to check whether the generator's own inputs satisfy
some group's constraints, only whether candidate and reference agree). This
is the same trick ``worker/pipelines/statement.py`` uses to compute a
missing sample answer - "let a solution answer, then let a checker judge it"
already covers everything needed once the two roles being compared are
handed to it.

Unlike every other pipeline in this package, the number of children is not
known when ``make_input_digest`` runs - it depends on when (or whether) a
mismatch turns up. Per CLAUDE.md's "Adding a pipeline" recipe: the step
table and child registry still hold every bit of decision state, but the
*count* of children is derived (``len(self._children)``), never stored
separately, and at most one child is ever unfinished at a time. Children are
named with a zero-padded index (``iter-00000007``, not ``iter-7``) because
``PipelineState`` round-trips through storage as ``sort_keys=True`` JSON
(see ``olympdev/storage/abstract.py``) - an unpadded ``"iter-9"`` would sort
after ``"iter-10"`` on restore, silently reordering the child dict. Indexing
by the exact computed key (never by list position) keeps this correct
either way; the padding is defense in depth for anything that later iterates
``children()`` and assumes insertion order.
"""

from typing import Self

from pydantic import model_validator

from olympdev.problem.types import ProblemConfiguration
from olympdev.schema import StorageModel
from olympdev.storage import AbstractStorage
from olympdev.worker.job import JobStatus
from olympdev.worker.jobs.compile import CompileJobResult, compile_job_class
from olympdev.worker.pipeline import (
    AbstractPipeline,
    NextJobStatus,
    PipelineContext,
    ScheduledJob,
)
from olympdev.worker.pipelines.problem_test import (
    ProblemTestArguments,
    ProblemTestPipeline,
    ProblemTestingCase,
    RunLimits,
    TestingContext,
)
from olympdev.worker.roles import ROLE_ORDER, ProgramRole
from olympdev.worker.verdict import Verdict

#: Stress needs a candidate to run, a reference to compare against, a
#: checker to compare them, and a generator to make growing-seed inputs.
#: No VALIDATOR - see the module docstring.
STRESS_REQUIRED_ROLES = frozenset(
    {ProgramRole.SOLUTION, ProgramRole.MODEL, ProgramRole.CHECKER, ProgramRole.GENERATOR}
)


class StressArguments(TestingContext, StorageModel):
    #: Compile-job *input* digest per role (same meaning as
    #: ``ProblemTestingArguments.compiles``) - SOLUTION is the candidate,
    #: MODEL the reference it is checked against. Each is actually compiled,
    #: once, by this pipeline's own compile phase (see ``_step_compiles``)
    #: before any iteration runs - a raw compile-job input is not a runnable
    #: binary digest.
    compiles: dict[ProgramRole, str]
    #: Generator argv shared by every iteration; each iteration appends its
    #: own seed as one more argument.
    base_gen_argv: list[str]
    seed_start: int = 1
    max_iterations: int

    @model_validator(mode="after")
    def _check_roles(self) -> Self:
        missing = STRESS_REQUIRED_ROLES - set(self.compiles)
        if missing:
            raise ValueError(
                "missing programs: " + ", ".join(sorted(role.value for role in missing))
            )
        if self.max_iterations < 1:
            raise ValueError("max_iterations must be positive")
        return self


class StressResult(StorageModel):
    found: bool
    iterations_run: int
    seed_argv: list[str] | None = None
    verdict: Verdict | None = None
    #: The generated test that produced the mismatch (None if none was found).
    input_digest: str | None = None
    candidate_run_digest: str | None = None
    reference_run_digest: str | None = None
    check_digest: str | None = None


class StressPipeline(AbstractPipeline):
    """Run growing-seed iterations of ``ProblemTestPipeline`` until the
    candidate and reference disagree, or the iteration budget runs out."""

    pipeline_type = "stress"

    def __init__(self, pipeline_id: str = "stress"):
        super().__init__(pipeline_id)
        # Caches only: both are re-derived from storage after a restore.
        self._arguments: StressArguments | None = None
        self._programs_cache: dict[ProgramRole, str] | None = None

    @classmethod
    def make_input_digest(
        cls,
        storage: AbstractStorage,
        *,
        compiles: dict[ProgramRole, str],
        configuration: ProblemConfiguration,
        base_gen_argv: list[str],
        max_iterations: int,
        seed_start: int = 1,
        problem_name: str | None = None,
        binary_names: dict[ProgramRole, str] | None = None,
    ) -> str:
        for digest in compiles.values():
            storage.validate_digest(digest)
        return StressArguments(
            compiles=compiles,
            limits=RunLimits.from_configuration(configuration),
            problem_name=problem_name,
            binary_names=binary_names or {},
            base_gen_argv=base_gen_argv,
            max_iterations=max_iterations,
            seed_start=seed_start,
        ).upload(storage)

    def _parse_arguments(self, context: PipelineContext) -> StressArguments:
        if self._arguments is None:
            args = StressArguments.require_from_storage(
                context.storage, context.input_digest, missing="missing pipeline input"
            )
            for digest in args.compiles.values():
                context.storage.validate_digest(digest)
            self._arguments = args
        return self._arguments

    def get_result(self, storage: AbstractStorage) -> StressResult | None:
        digest = self.get_output_digest()
        if digest is None:
            return None
        return StressResult.from_storage(storage, digest)

    @staticmethod
    def _child_key(index: int) -> str:
        # Zero-padded - see the module docstring on why this matters.
        return f"iter-{index:08d}"

    def _roles(self, args: StressArguments) -> list[ProgramRole]:
        return [role for role in ROLE_ORDER if role in args.compiles]

    def _step_compiles(
        self, context: PipelineContext, args: StressArguments
    ) -> ScheduledJob | NextJobStatus | None:
        if self._programs_cache is not None:
            return None
        for role in self._roles(args):
            step = f"compile-{role.value}"
            if not self.has_step(step):
                return self.issue(
                    step, compile_job_class(role), args.compiles[role], labels=args.labels(role)
                )
        if self.any_step_in_flight():
            return NextJobStatus.WAITING
        return self._finish_compiles(context.storage, args)

    def _finish_compiles(
        self, storage: AbstractStorage, args: StressArguments
    ) -> NextJobStatus | None:
        programs: dict[ProgramRole, str] = {}
        for role in self._roles(args):
            scheduled = self.step(f"compile-{role.value}")
            assert scheduled is not None
            if scheduled.job.get_status() is JobStatus.FAILED:
                self.fail(scheduled.job.get_failure_reason())
                return NextJobStatus.FINISHED
            digest = scheduled.job.get_output_digest()
            result = CompileJobResult.from_storage(storage, digest) if digest is not None else None
            if result is None or result.program_digest is None:
                self.fail(f"{role.value} compilation produced no program")
                return NextJobStatus.FINISHED
            programs[role] = result.program_digest
        self._programs_cache = programs
        return None

    def _next_job(self, context: PipelineContext) -> ScheduledJob | NextJobStatus:
        args = self._parse_arguments(context)
        storage = context.storage

        compile_step = self._step_compiles(context, args)
        if compile_step is not None:
            return compile_step

        count = len(self._children)

        if count > 0:
            last = self._children[self._child_key(count - 1)]
            if not last.pipeline.is_finished():
                job = self.pull_child(last, context)
                if self.is_finished():
                    return NextJobStatus.FINISHED
                if job is not None:
                    return job
                if not last.pipeline.is_finished():
                    return NextJobStatus.WAITING
            child = last.pipeline
            assert isinstance(child, ProblemTestPipeline)
            result = child.get_result(storage)
            if result is None:
                self.fail(f"missing result for iteration {count - 1}")
                return NextJobStatus.FINISHED
            if result.verdict is not Verdict.OK:
                return self._complete_found(storage, args, child, count - 1)
            if count >= args.max_iterations:
                return self._complete_exhausted(storage, count)

        return self._add_iteration(storage, context, args, count)

    def _add_iteration(
        self,
        storage: AbstractStorage,
        context: PipelineContext,
        args: StressArguments,
        index: int,
    ) -> ScheduledJob | NextJobStatus:
        seed = args.seed_start + index
        case = ProblemTestingCase(
            name=f"iter-{seed}",
            gen_argv=[*args.base_gen_argv, str(seed)],
        )
        assert self._programs_cache is not None
        test_args = ProblemTestArguments.from_parent(
            args, case, programs=self._programs_cache, index=index
        )
        child_digest = ProblemTestPipeline.make_input_digest(storage, test_args)
        scheduled = self.add_child(
            self._child_key(index), ProblemTestPipeline(self._child_key(index)), child_digest
        )
        job = self.pull_child(scheduled, context)
        if self.is_finished():
            return NextJobStatus.FINISHED
        return job if job is not None else NextJobStatus.WAITING

    def _complete_found(
        self,
        storage: AbstractStorage,
        args: StressArguments,
        child: ProblemTestPipeline,
        index: int,
    ) -> NextJobStatus:
        result = child.get_result(storage)
        assert result is not None
        self.complete(
            StressResult(
                found=True,
                iterations_run=index + 1,
                seed_argv=[*args.base_gen_argv, str(args.seed_start + index)],
                verdict=result.verdict,
                input_digest=child.generated_input_digest(storage),
                candidate_run_digest=result.run_digest,
                reference_run_digest=child.model_run_digest(),
                check_digest=result.check_digest,
            ).upload(storage)
        )
        return NextJobStatus.FINISHED

    def _complete_exhausted(self, storage: AbstractStorage, count: int) -> NextJobStatus:
        self.complete(StressResult(found=False, iterations_run=count).upload(storage))
        return NextJobStatus.FINISHED
