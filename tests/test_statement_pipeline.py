"""The statement pipeline: state round-trip, computed sample answers, and
(when a real LaTeX toolchain is reachable) an actual compiled PDF.

Exercised against the bundled ``example-contest/aplusb`` example, whose two
samples have no static ``output_file`` - the normal case, matching
``ProblemPackage.role_files``'s own "the model answers what a test doesn't
have a file for" rule (see rules.md and CLAUDE.md: Adding a pipeline). This
is exactly the scenario ``build_problem_statement_input`` must handle by
running the model/main solution rather than requiring a hand-written answer.
"""

import shutil
from pathlib import Path

import pytest

from olympdev.isolate import IsolateFactory
from olympdev.problem.build import (
    build_contest_statement_input,
    build_problem_statement_input,
)
from olympdev.problem.package import Contest, ProblemPackage
from olympdev.storage.filestorage import FileStorage
from olympdev.worker.pipeline import (
    AbstractPipeline,
    NextJobStatus,
    PipelineContext,
    PipelineState,
    PipelineStatus,
    ScheduledJob,
    run_pipeline,
)
from olympdev.worker.pipelines.statement import StatementBuildInput, StatementPipeline

pytestmark = pytest.mark.slow


def _latex_available() -> bool:
    """Whether a LaTeX compiler this job's own allowlist accepts is reachable.

    Mirrors ``worker/jobs/statement_compile.py``'s own check rather than
    re-deriving it, so this test degrades exactly where the job itself would.
    """
    from olympdev.worker.jobs.statement_compile import (
        _compiler_dir_allowed,
        _STATEMENT_COMPILER_NAME,
    )

    found = shutil.which("latexmk")
    if found is None:
        return False
    path = Path(found)
    return bool(_STATEMENT_COMPILER_NAME.fullmatch(path.name)) and _compiler_dir_allowed(path)


def test_statement_pipeline_state_roundtrips_before_any_job(storage: FileStorage):
    """No sandbox, no toolchain needed: a fresh pipeline's shape survives a
    save/restore before it has issued anything - see CLAUDE.md's "Adding a
    pipeline" recipe."""
    input_digest = StatementBuildInput(
        file_digests=[storage.upload_bytes(b"\\begin{document}x\\end{document}")],
        file_paths=["statement.tex"],
        command=["/usr/bin/latexmk", "-pdf", 0],
        output_relpath="statement.pdf",
    ).upload(storage)

    original = StatementPipeline("roundtrip")
    state_digest = original.save_state(input_digest).upload(storage)
    state = PipelineState.require_from_storage(storage, state_digest)
    restored = AbstractPipeline.restore(state, storage)

    assert isinstance(restored, StatementPipeline)
    assert restored.pipeline_id == "roundtrip"
    assert restored.get_status() is PipelineStatus.PENDING


def _drive_children_to_completion(
    pipeline: StatementPipeline, context: PipelineContext
) -> None:
    """Run every answer-computing child to completion, then stop.

    The moment the last child finishes, ``StatementPipeline._next_job``
    itself immediately tries to build the final compile job (see
    ``_next_job``'s single-pass loop) - there's no separate call at which to
    "peek" in between. On a host with no allow-listed LaTeX toolchain, that
    raises; that's expected here and is not this test's concern (see
    ``test_statement_pipeline_compiles_a_real_pdf`` for the compile step).
    """
    for _ in range(500):
        if pipeline._children and all(
            scheduled.pipeline.is_finished() for scheduled in pipeline.children()
        ):
            return
        try:
            nxt = pipeline.get_next_job(context)
        except ValueError as exc:
            if "statement compil" not in str(exc):
                raise
            return
        assert isinstance(nxt, ScheduledJob), nxt
        nxt.job.execute(context.job_context(nxt.input_digest))
    pytest.fail("answer-computing children did not finish")


def test_statement_pipeline_computes_missing_sample_answers(
    storage: FileStorage, isolate_factory: IsolateFactory, aplusb_dir: Path
):
    """aplusb's two samples have no output_file - build_problem_statement_input
    must compute their answers by running the main solution, not fail or
    silently omit them (see rules.md §8's MUST: examples with an explanation)."""
    if shutil.which("g++") is None:
        pytest.skip("g++ is not available")
    package = ProblemPackage.load(aplusb_dir)
    digest = build_problem_statement_input(storage, package, lang="en")
    pipeline = StatementPipeline("aplusb-en")
    context = PipelineContext(
        input_digest=digest, storage=storage, isolate_factory=isolate_factory
    )
    args = pipeline._parse_arguments(context)
    assert len(args.answer_testing_digests) == 1  # one fragment (problem), two samples in it

    _drive_children_to_completion(pipeline, context)

    answer_digests, answer_paths = pipeline._collect_answers(storage, args)
    assert answer_digests is not None
    assert answer_paths == ["sample-0.out", "sample-1.out"]
    assert storage.download_bytes(answer_digests[0]) == b"13\n"
    assert storage.download_bytes(answer_digests[1]) == b"993\n"


@pytest.mark.skipif(not _latex_available(), reason="no allow-listed latexmk on PATH")
def test_statement_pipeline_compiles_a_real_pdf(
    storage: FileStorage, isolate_factory: IsolateFactory, aplusb_dir: Path
):
    """``en`` is used here (not ``ru``) because it needs nothing of the host
    beyond a TeX install. ``ru``'s T2A/Cyrillic metrics are generated on
    demand by ``mktextfm``, which cannot run in the sandbox; the statement
    profile binds the host's ``$TEXMFVAR`` read-only so already-generated
    ones are found (see ``isolate/profiles.py``), but whether a given metric
    was ever generated - and whether that cache is readable by the sandbox
    user - is host provisioning, not olympdev's plumbing. See
    ``tests/test_statement_rendering.py`` for a sandbox-free check that the
    ``ru`` document itself renders with the right macros and language."""
    if shutil.which("g++") is None:
        pytest.skip("g++ is not available")
    package = ProblemPackage.load(aplusb_dir)
    digest = build_problem_statement_input(storage, package, lang="en")
    pipeline = StatementPipeline("aplusb-en")
    context = PipelineContext(
        input_digest=digest, storage=storage, isolate_factory=isolate_factory
    )
    run_pipeline(pipeline, context)

    assert pipeline.get_status() is PipelineStatus.COMPLETED, pipeline.get_failure_reason()
    pdf_digest = pipeline.get_output_digest()
    assert pdf_digest is not None
    pdf_bytes = storage.download_bytes(pdf_digest)
    assert pdf_bytes is not None and pdf_bytes.startswith(b"%PDF")

    # TeX's own log is kept whatever the outcome: on a failure it is the only
    # place the error names a .tex file and a line.
    compile_result = pipeline.compile_result(storage)
    assert compile_result is not None and compile_result.log_digest is not None
    log_bytes = storage.download_bytes(compile_result.log_digest)
    assert log_bytes is not None and b"This is pdfTeX" in log_bytes

    # Restored, this already-finished pipeline must report FINISHED right
    # away rather than re-issuing anything. (Comparing job-id sets rather
    # than an exact dict/list order: PipelineState's dicts round-trip
    # through storage as sort_keys=True JSON, so children/step iteration
    # order is not preserved across a restore - see
    # worker/pipelines/stress.py's module docstring.)
    executed_before = {
        step.job.job_id
        for scheduled in pipeline._children.values()
        for step in scheduled.pipeline._steps.values()
    } | {step.job.job_id for step in pipeline._steps.values()}
    state_digest = pipeline.save_state(digest).upload(storage)
    state = PipelineState.require_from_storage(storage, state_digest)
    restored = AbstractPipeline.restore(state, storage)
    assert isinstance(restored, StatementPipeline)
    assert restored.get_status() is PipelineStatus.COMPLETED
    assert restored.get_output_digest() == pdf_digest
    assert restored.get_next_job(context) is NextJobStatus.FINISHED
    executed_after = {
        step.job.job_id
        for scheduled in restored._children.values()
        for step in scheduled.pipeline._steps.values()
    } | {step.job.job_id for step in restored._steps.values()}
    assert executed_before == executed_after


def test_contest_statement_typesets_every_problem_into_one_pdf(
    storage: FileStorage, isolate_factory: IsolateFactory
):
    """A whole-contest build is one pipeline and one PDF, not a PDF per
    problem stapled together - so a sample whose answer must be computed is
    handled the same way it is for a single problem."""
    if shutil.which("g++") is None:
        pytest.skip("g++ is not available")
    if not _latex_available():
        pytest.skip("no allowed LaTeX compiler is reachable")
    contest_dir = Path(__file__).resolve().parent.parent / "example-contest"
    contest = Contest.load(contest_dir)
    digest = build_contest_statement_input(storage, contest, lang="en")
    pipeline = StatementPipeline("example-contest-en")
    run_pipeline(
        pipeline,
        PipelineContext(
            input_digest=digest, storage=storage, isolate_factory=isolate_factory
        ),
    )
    assert pipeline.get_status() is PipelineStatus.COMPLETED, (
        pipeline.get_failure_reason()
    )
    pdf_digest = pipeline.get_output_digest()
    assert pdf_digest is not None
    pdf_bytes = storage.download_bytes(pdf_digest)
    assert pdf_bytes is not None and pdf_bytes.startswith(b"%PDF")


def test_contest_statement_names_the_problem_missing_a_language(
    storage: FileStorage,
):
    """example-contest's `points` has no `ru` statement. A booklet cannot be
    half-typeset, so this is refused up front and says which problem."""
    contest_dir = Path(__file__).resolve().parent.parent / "example-contest"
    contest = Contest.load(contest_dir)
    with pytest.raises(ValueError, match="points: no statement for lang 'ru'"):
        build_contest_statement_input(storage, contest, lang="ru")
