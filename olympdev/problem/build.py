"""Turning a loaded problem into a pipeline input.

The verbose part of setting up a testing run used to be here, in the caller:
one (sources, command) pair per role, testlib.h located and attached by hand,
compiler and flags spelled out five times. All of that is derived - from the
roles the problem needs, the toolchain settings for each source's language,
and whether the role is a testlib tool.
"""

from __future__ import annotations

from contextlib import ExitStack
from pathlib import Path

from olympdev.problem.package import ProblemPackage
from olympdev.problem.types import ProblemSolution
from olympdev.resources import asset_file
from olympdev.settings import Settings, get_settings, language_for_source
from olympdev.storage import AbstractStorage
from olympdev.worker.pipelines.problem_testing import CompileSpec, ProblemTestingPipeline
from olympdev.worker.pipelines.problem_verification import (
    ProblemVerificationPipeline,
    SolutionVerificationSpec,
)
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
            SolutionVerificationSpec(tag=resolved.tag, testing_digest=testing_digest)
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
