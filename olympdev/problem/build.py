"""Turning a loaded problem into a pipeline input.

The verbose part of setting up a testing run used to be here, in the caller:
one (sources, command) pair per role, testlib.h located and attached by hand,
compiler and flags spelled out five times. All of that is derived - from the
roles the problem needs, the toolchain settings for each source's language,
and whether the role is a testlib tool.
"""

from __future__ import annotations

import shlex
from contextlib import ExitStack
from pathlib import Path

from olympdev.problem.package import Contest, ProblemPackage
from olympdev.problem.statement import (
    OLYMP_STY_ASSET,
    pending_sample_tests,
    read_samples,
    render_contest_document,
    render_problem_fragment,
    sample_sources,
    stage_asset_dir,
)
from olympdev.problem.types import (
    ContestStatementConfig,
    ProblemConfiguration,
    ProblemSolution,
    ProblemSourceType,
    ProblemTest,
    StatementSpec,
    parse_gen_argv,
)
from olympdev.resources import asset_file
from olympdev.settings import (
    Settings,
    StatementEngine,
    StatementToolchainSettings,
    get_settings,
    language_for_source,
)
from olympdev.storage import AbstractStorage
from olympdev.worker.jobs.compile import compile_job_class
from olympdev.worker.pipelines.problem_testing import CompileSpec, ProblemTestingPipeline
from olympdev.worker.pipelines.problem_verification import (
    ProblemVerificationPipeline,
    SolutionVerificationSpec,
)
from olympdev.worker.pipelines.statement import StatementPipeline
from olympdev.worker.pipelines.stress import StressPipeline
from olympdev.worker.roles import ROLE_SPECS, ProgramRole

TESTLIB_HEADER = "testlib.h"

#: Roles written against testlib, which therefore need its header in the box.
TESTLIB_ROLES = frozenset(
    {ProgramRole.CHECKER, ProgramRole.GENERATOR, ProgramRole.VALIDATOR}
)


def _sandbox_source_name(role: ProgramRole, source: Path | bytes) -> str:
    """Name the source gets inside the sandbox."""
    if isinstance(source, bytes):
        return f"{ROLE_SPECS[role].binary_name}.cpp"
    return source.name


def compile_spec(
    role: ProgramRole,
    source: Path | bytes,
    *,
    testlib_path: Path | None = None,
    settings: Settings | None = None,
    suffix_hint: str = ".cpp",
) -> CompileSpec:
    """Sources and command that build one role's binary.

    The compiler and flags come from the toolchain configured for the
    source's language; testlib.h is attached for the roles that need it.
    """
    settings = settings or get_settings()
    name = _sandbox_source_name(role, source)
    language = language_for_source(name if Path(name).suffix else f"x{suffix_hint}")
    toolchain = settings.toolchain(language)
    compiler = toolchain.resolve_compiler(language)
    sources: list[tuple[Path | bytes, str | Path]] = [(source, name)]
    if role in TESTLIB_ROLES:
        if testlib_path is None:
            raise ValueError(f"{role.value} needs testlib.h; pass testlib_path")
        sources.append((testlib_path, TESTLIB_HEADER))
    return CompileSpec(
        sources=sources,
        command=[compiler, *toolchain.flags, "-o", ROLE_SPECS[role].binary_name, 0],
    )


def build_problem_testing_input(
    storage: AbstractStorage,
    package: ProblemPackage,
    *,
    solution: ProblemSolution | Path | bytes | str | None = None,
    settings: Settings | None = None,
    problem_name: str | None = None,
) -> str:
    """Upload everything one testing run needs and return the pipeline input digest.

    ``solution`` defaults to the problem's main solution; it may also be a tag
    from info.json, a path, or raw source bytes (a submission).
    """
    settings = settings or get_settings()
    if isinstance(solution, ProblemSolution):
        solution = solution.file
    role_files = package.role_files(solution=solution)
    needs_testlib = bool(TESTLIB_ROLES & set(role_files))
    with ExitStack() as stack:
        testlib_path = (
            stack.enter_context(asset_file(TESTLIB_HEADER)) if needs_testlib else None
        )
        programs = {
            role: compile_spec(
                role, source, testlib_path=testlib_path, settings=settings
            )
            for role, source in role_files.items()
        }
        return ProblemTestingPipeline.make_input_digest(
            storage,
            programs=programs,
            configuration=package.configuration,
            problem_dir=package.directory,
            problem_name=problem_name or package.name,
        )


def _resolve_verifiable(
    package: ProblemPackage, solution: ProblemSolution | str
) -> ProblemSolution:
    resolved = package.solution(solution) if isinstance(solution, str) else solution
    if resolved.is_ignored():
        raise ValueError(
            f"{package.name}: solution tagged {resolved.tag!r} is ignored"
        )
    return resolved


def build_solution_verification_input(
    storage: AbstractStorage,
    package: ProblemPackage,
    *,
    solution: ProblemSolution | str,
    settings: Settings | None = None,
    problem_name: str | None = None,
) -> str:
    """Upload one solution's testing input plus its tag for verification."""
    settings = settings or get_settings()
    resolved = _resolve_verifiable(package, solution)
    testing_digest = build_problem_testing_input(
        storage,
        package,
        solution=resolved,
        settings=settings,
        problem_name=problem_name,
    )
    return ProblemVerificationPipeline.make_input_digest(
        storage,
        solutions=[
            SolutionVerificationSpec(
                name=resolved.name,
                tag=resolved.tag,
                testing_digest=testing_digest,
            )
        ],
    )


def build_problem_verification_input(
    storage: AbstractStorage,
    package: ProblemPackage,
    *,
    settings: Settings | None = None,
    problem_name: str | None = None,
) -> str:
    """Upload every non-ignored solution for a full-problem verification run."""
    settings = settings or get_settings()
    solutions = package.verifiable_solutions()
    if not solutions:
        raise ValueError(f"{package.name}: no solutions to verify")
    return ProblemVerificationPipeline.make_input_digest(
        storage,
        solutions=[
            SolutionVerificationSpec(
                name=item.name,
                tag=item.tag,
                testing_digest=build_problem_testing_input(
                    storage,
                    package,
                    solution=item,
                    settings=settings,
                    problem_name=problem_name,
                ),
            )
            for item in solutions
        ],
    )


def _statement_fragment_sources(
    package: ProblemPackage, spec: StatementSpec
) -> tuple[str, list[tuple[Path | bytes, str]], list[tuple[str, ProblemTest]]]:
    """Render one problem's fragment, the sample/asset files it needs staged,
    and any samples whose answer still needs computing (see
    ``_build_answer_batch``)."""
    body = package.file(spec.source).read_text(encoding="utf-8")
    samples = read_samples(package)
    template_text = (
        package.file(spec.template).read_text(encoding="utf-8")
        if spec.template is not None
        else None
    )
    fragment = render_problem_fragment(
        spec=spec,
        time_limit_ms=package.configuration.time_limit_ms,
        memory_limit_mb=package.configuration.memory_limit_mb,
        body=body,
        samples=samples,
        groups=package.configuration.groups if package.configuration.mode is not None else [],
        template_text=template_text,
    )
    sources: list[tuple[Path | bytes, str]] = list(sample_sources(samples))
    for directory in spec.assets:
        sources.extend(stage_asset_dir(package.asset_dir(directory)))
    return fragment, sources, pending_sample_tests(samples)


def _build_answer_batch(
    storage: AbstractStorage,
    package: ProblemPackage,
    pending: list[tuple[str, ProblemTest]],
    *,
    settings: Settings,
) -> str:
    """Compute a problem's missing sample answers by testing its own
    model/main solution against itself, one synthetic test per pending
    sample (same ``input_file``, ``name=stem``).

    Reuses ``ProblemTestingPipeline`` wholesale - compile, validate (when the
    problem has a validator), run, and check - rather than a bespoke
    "just run and grab stdout" job: the checker accepting the model's own
    answer is itself a sanity check that the sample computed cleanly, and
    every existing TL/ML/RE/CF failure mode is already handled by it. Only
    the ``ProblemTestPipeline.RUN`` step's stdout is used; MODEL is set to
    the same binary as SOLUTION purely so the pipeline has an answer to
    check the run against.
    """
    role_files = package.role_files()
    programs_roles: dict[ProgramRole, Path | bytes] = {
        ProgramRole.SOLUTION: role_files[ProgramRole.SOLUTION],
        ProgramRole.MODEL: role_files[ProgramRole.SOLUTION],
        ProgramRole.CHECKER: role_files[ProgramRole.CHECKER],
    }
    if ProgramRole.VALIDATOR in role_files:
        programs_roles[ProgramRole.VALIDATOR] = role_files[ProgramRole.VALIDATOR]
    needs_testlib = bool(TESTLIB_ROLES & set(programs_roles))
    synthetic_tests = [
        ProblemTest(input_file=test.input_file, name=stem) for stem, test in pending
    ]
    synthetic_configuration = ProblemConfiguration(
        time_limit_ms=package.configuration.time_limit_ms,
        memory_limit_mb=package.configuration.memory_limit_mb,
        samples=synthetic_tests,
    )
    with ExitStack() as stack:
        testlib_path = (
            stack.enter_context(asset_file(TESTLIB_HEADER)) if needs_testlib else None
        )
        programs = {
            role: compile_spec(role, source, testlib_path=testlib_path, settings=settings)
            for role, source in programs_roles.items()
        }
        return ProblemTestingPipeline.make_input_digest(
            storage,
            programs=programs,
            configuration=synthetic_configuration,
            problem_dir=package.directory,
            problem_name=package.name,
        )


def _statement_compile_command(
    toolchain: StatementToolchainSettings, engine: StatementEngine
) -> list[str | int]:
    """The compile command for one engine, with an explicit path for any
    sub-tool the top-level compiler would otherwise have to find on PATH.

    The compile sandbox's PATH is fixed and narrow (see
    ``isolate/profiles.py``'s ``COMPILE_PROFILE``) - a TeX Live ``latexmk``
    spawns ``pdflatex`` as a child process, which then has to be found by
    name unless told exactly where it is. TeX Live always installs every
    engine's binaries side by side, so the sibling in ``latexmk``'s own
    resolved directory is used instead of widening the sandbox's environment.
    """
    compiler_path = Path(toolchain.resolve_command(engine))
    command: list[str | int] = [str(compiler_path)]
    if compiler_path.name == "latexmk":
        pdflatex_path = compiler_path.with_name("pdflatex")
        if pdflatex_path.is_file():
            command.append(f"-pdflatex={pdflatex_path}")
    command.extend(toolchain.args)
    command.append(0)
    return command


def _build_statement_document_input(
    storage: AbstractStorage,
    *,
    fragments: list[str],
    fragment_sources: list[list[tuple[Path | bytes, str]]],
    answer_testing_digests: list[str],
    contest_statement: ContestStatementConfig,
    engine: StatementEngine,
    lang: str,
    settings: Settings,
    contest: Contest | None = None,
) -> str:
    """Shared by a single-problem and a whole-contest build: render the
    document shell, assemble every source, and issue the compile pipeline."""
    template_text = None
    if contest_statement.template is not None:
        if contest is None:
            raise ValueError("a contest-level template override needs a Contest")
        template_text = contest.file(contest_statement.template).read_text(
            encoding="utf-8"
        )
    document = render_contest_document(
        contest=contest_statement,
        fragments=fragments,
        template_text=template_text,
        lang=lang,
    )
    sources: list[tuple[Path | bytes, str]] = [
        (document.encode("utf-8"), "statement.tex")
    ]
    with asset_file(OLYMP_STY_ASSET) as olymp_sty_path:
        sources.append((olymp_sty_path, "olymp.sty"))
        for one_fragment_sources in fragment_sources:
            sources.extend(one_fragment_sources)
        if contest is not None:
            for directory in contest_statement.assets:
                sources.extend(stage_asset_dir(contest.asset_dir(directory)))
        command = _statement_compile_command(settings.statement_toolchain(engine), engine)
        return StatementPipeline.make_input_digest(
            storage,
            sources=sources,
            command=command,
            output_relpath="statement.pdf",
            answer_testing_digests=answer_testing_digests,
        )


def build_problem_statement_input(
    storage: AbstractStorage,
    package: ProblemPackage,
    *,
    lang: str,
    settings: Settings | None = None,
    contest_statement: ContestStatementConfig | None = None,
) -> str:
    """Upload one problem's statement and return the pipeline input digest.

    Always renders through the same contest document shell a whole-contest
    build uses; a standalone problem synthesizes a minimal contest (its own
    title, no location/date/template/assets) when none is given.
    """
    settings = settings or get_settings()
    spec = package.statement_spec(lang)
    contest_statement = contest_statement or ContestStatementConfig(
        lang=lang, title=spec.title, date=""
    )
    fragment, sources, pending = _statement_fragment_sources(package, spec)
    answer_testing_digests = (
        [_build_answer_batch(storage, package, pending, settings=settings)]
        if pending
        else []
    )
    return _build_statement_document_input(
        storage,
        fragments=[fragment],
        fragment_sources=[sources],
        answer_testing_digests=answer_testing_digests,
        contest_statement=contest_statement,
        engine=spec.engine,
        lang=lang,
        settings=settings,
    )


def build_contest_statement_input(
    storage: AbstractStorage,
    contest: Contest,
    *,
    lang: str,
    settings: Settings | None = None,
) -> str:
    """Upload a whole contest's statement (every problem, one language) and
    return the pipeline input digest."""
    settings = settings or get_settings()
    # Title, venue and date are printed, so each language has its own.
    contest_statement = contest.statement_config(lang)
    fragments: list[str] = []
    fragment_sources: list[list[tuple[Path | bytes, str]]] = []
    answer_testing_digests: list[str] = []
    engine: StatementEngine | None = None
    for name, package in contest.problems.items():
        try:
            spec = package.statement_spec(lang)
        except KeyError:
            raise ValueError(
                f"{name}: no statement for lang {lang!r} in this contest build"
            ) from None
        if engine is None:
            engine = spec.engine
        elif engine != spec.engine:
            raise ValueError(
                f"{name}: statement engine {spec.engine.value!r} does not match "
                f"the rest of the contest ({engine.value!r})"
            )
        fragment, sources, pending = _statement_fragment_sources(package, spec)
        fragments.append(fragment)
        fragment_sources.append(sources)
        if pending:
            answer_testing_digests.append(
                _build_answer_batch(storage, package, pending, settings=settings)
            )
    assert engine is not None  # Contest.load requires a non-empty problems list
    return _build_statement_document_input(
        storage,
        fragments=fragments,
        fragment_sources=fragment_sources,
        answer_testing_digests=answer_testing_digests,
        contest_statement=contest_statement,
        engine=engine,
        lang=lang,
        settings=settings,
        contest=contest,
    )


def _resolve_solution_file(package: ProblemPackage, name_or_path: str) -> str:
    """A stress role may be named either way - "sol-wa" (a solution's file
    stem) or a bare path - mirroring how a notebook cell already lets a
    caller name a solution by its short name (see ``ProblemWorkbench._resolve``).
    """
    solution = package.configuration.solution_by_name(name_or_path)
    return solution.file if solution is not None else name_or_path


def _gen_argv_for_group(configuration: ProblemConfiguration, group: int) -> list[str]:
    """The argv of the first generated test tagged with this group - a stress
    run reuses a real test's own generator invocation rather than inventing
    a new one, so a seed always lands in the group it's meant to stress."""
    for test in configuration.tests:
        if test.group_id == group and test.gen_cmd is not None:
            return parse_gen_argv(test.gen_cmd)
    raise ValueError(f"no generated test found for group {group}")


def build_stress_input(
    storage: AbstractStorage,
    package: ProblemPackage,
    *,
    candidate: str,
    against: str = "model",
    group: int | None = None,
    gen_args: str | None = None,
    max_iterations: int = 1000,
    seed_start: int = 1,
    settings: Settings | None = None,
    problem_name: str | None = None,
) -> str:
    """Upload everything a stress run needs and return the pipeline input digest.

    ``against="model"`` (the default) means the problem's main solution - a
    named convention, not a literal ``ProgramRole`` lookup, distinct from (if
    usually equal to) the testing pipeline's own optional model-answer role.
    Any other string names a solution the same way ``candidate`` does.

    Exactly one of ``group`` (reuse a real generated test's own argv for that
    group) or ``gen_args`` (a raw, literal argv string - no dropped binary
    name, since there is nothing analogous to ``gen_cmd``'s leading token
    here) must be given.
    """
    settings = settings or get_settings()
    if (group is None) == (gen_args is None):
        raise ValueError("stress needs exactly one of group or gen_args")
    if gen_args is not None:
        base_gen_argv = shlex.split(gen_args)
        if not base_gen_argv:
            raise ValueError("empty gen_args")
    else:
        assert group is not None
        base_gen_argv = _gen_argv_for_group(package.configuration, group)

    candidate_file = _resolve_solution_file(package, candidate)
    reference_file = (
        package.main_solution().file
        if against == "model"
        else _resolve_solution_file(package, against)
    )
    checker_source = package.source(ProblemSourceType.CHECK)
    if checker_source is None:
        raise ValueError(f"{package.name}: a checker source is required for stress")
    generator_source = package.source(ProblemSourceType.GEN)
    if generator_source is None:
        raise ValueError(f"{package.name}: a generator source is required for stress")

    with ExitStack() as stack:
        testlib_path = stack.enter_context(asset_file(TESTLIB_HEADER))
        programs = {
            ProgramRole.SOLUTION: compile_spec(
                ProgramRole.SOLUTION, package.file(candidate_file), settings=settings
            ),
            ProgramRole.MODEL: compile_spec(
                ProgramRole.MODEL, package.file(reference_file), settings=settings
            ),
            ProgramRole.CHECKER: compile_spec(
                ProgramRole.CHECKER,
                package.file(checker_source.file),
                testlib_path=testlib_path,
                settings=settings,
            ),
            ProgramRole.GENERATOR: compile_spec(
                ProgramRole.GENERATOR,
                package.file(generator_source.file),
                testlib_path=testlib_path,
                settings=settings,
            ),
        }
        compiles = {
            role: compile_job_class(role).make_input_digest(
                storage, sources=spec.sources, command=spec.command
            )
            for role, spec in programs.items()
        }
        binary_names = {
            role: name
            for role, spec in programs.items()
            if (name := spec.binary_display_name()) is not None
        }
        return StressPipeline.make_input_digest(
            storage,
            compiles=compiles,
            configuration=package.configuration,
            base_gen_argv=base_gen_argv,
            max_iterations=max_iterations,
            seed_start=seed_start,
            problem_name=problem_name or package.name,
            binary_names=binary_names,
        )
