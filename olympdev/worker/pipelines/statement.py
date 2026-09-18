"""Compiling one statement bundle - a single problem, or a whole contest.

Most sources (the rendered document, olymp.sty, samples already on disk) are
known and already uploaded by the time this pipeline starts - it just issues
the compile job for them. But a sample with no static answer needs one
computed first: the worker never touches a host path (see
``worker/pipelines/statement_compile.py``), so this pipeline runs a
``ProblemTestingPipeline`` child per problem that needs it (the problem's own
model/main solution tested against itself, one synthetic test per pending
sample - see ``problem/build.py``'s ``_build_answer_batch``) and only then
assembles the final compile job's file list, exactly the way
``ProblemVerificationPipeline`` nests one ``ProblemTestingPipeline`` child per
solution it verifies.
"""

from pathlib import Path
from typing import Sequence

from olympdev.isolate import COMPILE_RESERVED_PATHS, sandbox_relpath
from olympdev.schema import StorageModel
from olympdev.storage import AbstractStorage
from olympdev.worker.jobs.sandbox import resolve_sandbox_files
from olympdev.worker.jobs.simple_run import SimpleRunJobResult
from olympdev.worker.jobs.statement_compile import StatementCompileJob, StatementCompileResult
from olympdev.worker.pipeline import (
    AbstractPipeline,
    NextJobStatus,
    PipelineContext,
    ScheduledJob,
    ScheduledPipeline,
)
from olympdev.worker.pipelines.problem_testing import ProblemTestingPipeline
from olympdev.worker.verdict import Verdict

_COMPILE_STEP = "compile"


class StatementBuildInput(StorageModel):
    #: Every static source the compile needs, already uploaded - the
    #: rendered document, olymp.sty, samples with a known output, assets.
    file_digests: list[str]
    file_paths: list[str]
    command: list[str | int]
    output_relpath: str
    #: One ProblemTestingPipeline input per problem with samples whose
    #: answer must be computed - see ``problem/build.py``'s
    #: ``_build_answer_batch``. Each synthetic test's own name IS its
    #: sandbox stem ("sample-0", ...), so results need no separate mapping.
    answer_testing_digests: list[str] = []


class StatementPipeline(AbstractPipeline):
    pipeline_type = "statement"

    def __init__(self, pipeline_id: str = "statement"):
        super().__init__(pipeline_id)
        # Cache only: re-derived from storage after a restore.
        self._arguments: StatementBuildInput | None = None

    @classmethod
    def make_input_digest(
        cls,
        storage: AbstractStorage,
        *,
        sources: list[tuple[Path | bytes, str | Path]],
        command: list[str | int],
        output_relpath: str | Path,
        answer_testing_digests: Sequence[str] = (),
    ) -> str:
        file_digests, file_paths = resolve_sandbox_files(
            storage, files=sources, reserved=COMPILE_RESERVED_PATHS
        )
        for digest in answer_testing_digests:
            storage.validate_digest(digest)
        return StatementBuildInput(
            file_digests=file_digests,
            file_paths=file_paths,
            command=command,
            output_relpath=str(
                sandbox_relpath(output_relpath, reserved=COMPILE_RESERVED_PATHS)
            ),
            answer_testing_digests=list(answer_testing_digests),
        ).upload(storage)

    def _parse_arguments(self, context: PipelineContext) -> StatementBuildInput:
        if self._arguments is None:
            args = StatementBuildInput.require_from_storage(
                context.storage, context.input_digest, missing="missing pipeline input"
            )
            for digest in args.file_digests:
                context.storage.validate_digest(digest)
            for digest in args.answer_testing_digests:
                context.storage.validate_digest(digest)
            self._arguments = args
        return self._arguments

    def _next_job(self, context: PipelineContext) -> ScheduledJob | NextJobStatus:
        args = self._parse_arguments(context)
        storage = context.storage

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

        if not self.has_step(_COMPILE_STEP):
            answer_digests, answer_paths = self._collect_answers(storage, args)
            if answer_digests is None:
                return NextJobStatus.FINISHED
            compile_digest = StatementCompileJob.make_input_digest(
                storage,
                file_digests=list(
                    zip(
                        [*args.file_digests, *answer_digests],
                        [*args.file_paths, *answer_paths],
                    )
                ),
                command=args.command,
                output_relpath=args.output_relpath,
            )
            return self.issue(_COMPILE_STEP, StatementCompileJob, compile_digest)

        if self.step_in_flight(_COMPILE_STEP):
            return NextJobStatus.WAITING

        result, _ = self.step_result(
            _COMPILE_STEP,
            StatementCompileResult,
            storage,
            missing="missing statement compile result",
        )
        if result is None:
            return NextJobStatus.FINISHED

        self.complete(result.pdf_digest)
        return NextJobStatus.FINISHED

    def compile_result(self, storage: AbstractStorage) -> StatementCompileResult | None:
        """The compile job's own result, success or failure.

        The pipeline's output digest is the PDF, which a failed build does not
        have - but its log and output streams are exactly what a failed build
        needs to be diagnosable, so they are reachable from here. Read out of
        the step table, so a restored pipeline finds them too.
        """
        step = self.step(_COMPILE_STEP)
        if step is None:
            return None
        digest = step.job.get_output_digest()
        if digest is None:
            return None
        return StatementCompileResult.from_storage(storage, digest)

    def _ensure_children(self, args: StatementBuildInput) -> list[ScheduledPipeline]:
        if not self._children:
            for index, digest in enumerate(args.answer_testing_digests):
                self.add_child(
                    f"answers-{index}", ProblemTestingPipeline(f"answers-{index}"), digest
                )
        return self.children()

    def _collect_answers(
        self, storage: AbstractStorage, args: StatementBuildInput
    ) -> tuple[list[str], list[str]] | tuple[None, None]:
        digests: list[str] = []
        paths: list[str] = []
        for scheduled in self.children():
            pipeline = scheduled.pipeline
            assert isinstance(pipeline, ProblemTestingPipeline)
            result = pipeline.get_result(storage)
            if result is None:
                self.fail(f"missing computed answers for {pipeline.pipeline_id}")
                return None, None
            for test in result.tests:
                if test.verdict is not Verdict.OK:
                    self.fail(
                        f"could not compute sample answer {test.name}: {test.verdict.value}"
                    )
                    return None, None
                run_result = (
                    SimpleRunJobResult.from_storage(storage, test.run_digest)
                    if test.run_digest is not None
                    else None
                )
                if run_result is None:
                    self.fail(f"missing run result for sample {test.name}")
                    return None, None
                digests.append(run_result.stdout_digest)
                paths.append(f"{test.name}.out")
        return digests, paths
