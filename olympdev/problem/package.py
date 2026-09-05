"""Loading a problem (or a whole contest) from disk.

This is the layer between files on disk and the worker: it reads info.json,
resolves the files a problem refers to, and answers "which roles does this
problem need". Everything above it works with a ``ProblemPackage`` rather than
with paths, and everything below it works with digests.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Self

from olympdev.problem.types import (
    ProblemConfiguration,
    ProblemSolution,
    ProblemSource,
    ProblemSourceType,
    resolve_problem_file,
)
from olympdev.worker.roles import ProgramRole

INFO_FILE_NAME = "info.json"
CONTEST_FILE_NAME = "olympdev.json"

#: Which source type backs which role.
SOURCE_ROLES: dict[ProblemSourceType, ProgramRole] = {
    ProblemSourceType.CHECK: ProgramRole.CHECKER,
    ProblemSourceType.GEN: ProgramRole.GENERATOR,
    ProblemSourceType.VALID: ProgramRole.VALIDATOR,
}


def _load_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise FileNotFoundError(f"configuration file not found: {path}") from None
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON in {path}: {exc}") from None
    if not isinstance(data, dict):
        raise ValueError(f"expected a JSON object in {path}")
    return data


class ProblemPackage:
    """One problem directory plus its validated configuration."""

    def __init__(self, directory: Path, configuration: ProblemConfiguration):
        self.directory = directory
        self.configuration = configuration

    @property
    def name(self) -> str:
        return self.directory.name

    @classmethod
    def load(cls, path: Path | str) -> Self:
        """Load from a problem directory or directly from its info.json."""
        path = Path(path)
        info_path = path if path.is_file() else path / INFO_FILE_NAME
        configuration = ProblemConfiguration.model_validate(_load_json(info_path))
        return cls(info_path.parent.resolve(), configuration)

    # --- files ---------------------------------------------------------

    def file(self, relative: str) -> Path:
        """Resolve a path inside the problem, rejecting escapes and symlinks."""
        return resolve_problem_file(self.directory, relative)

    def source(self, source_type: ProblemSourceType) -> ProblemSource | None:
        return self.configuration.source(source_type)

    def solution(self, tag: str) -> ProblemSolution:
        solution = self.configuration.solution(tag)
        if solution is None:
            available = ", ".join(
                sorted(item.tag for item in self.configuration.solutions)
            ) or "none"
            raise KeyError(f"no solution tagged {tag!r} in {self.name} (have: {available})")
        return solution

    def main_solution(self) -> ProblemSolution:
        return self.solution("main")

    def solutions(self) -> list[ProblemSolution]:
        return list(self.configuration.solutions)

    # --- roles ---------------------------------------------------------

    def role_files(self, *, solution: Path | bytes | str | None = None) -> dict[
        ProgramRole, Path | bytes
    ]:
        """Which program backs each role for one testing run.

        The set is derived from the problem, not passed in: a checker/generator/
        validator listed in `sources` becomes that role, and a model solution is
        added exactly when some test has no answer file. A role that cannot be
        satisfied is a load-time error rather than a pipeline failure later.
        """
        if solution is None:
            solution = self.main_solution().file
        files: dict[ProgramRole, Path | bytes] = {
            ProgramRole.SOLUTION: (
                solution if isinstance(solution, bytes) else self.file(str(solution))
            )
        }
        for source_type, role in SOURCE_ROLES.items():
            source = self.source(source_type)
            if source is not None:
                files[role] = self.file(source.file)
        if ProgramRole.CHECKER not in files:
            raise ValueError(
                f"{self.name}: a checker source is required "
                f'(add {{"file": ..., "type": "check"}} to sources)'
            )
        if self.configuration.needs_model_solution():
            files[ProgramRole.MODEL] = self.file(self.main_solution().file)
        if self.uses_generated_tests() and ProgramRole.GENERATOR not in files:
            raise ValueError(
                f"{self.name}: tests use gen_cmd but no generator source is listed"
            )
        return files

    def uses_generated_tests(self) -> bool:
        return any(
            test.gen_cmd is not None
            for test in (*self.configuration.samples, *self.configuration.tests)
        )


class Contest:
    """A contest directory: olympdev.json listing problem subdirectories."""

    def __init__(self, directory: Path, problems: dict[str, ProblemPackage]):
        self.directory = directory
        self.problems = problems

    @classmethod
    def load(cls, path: Path | str) -> Self:
        path = Path(path)
        config_path = path if path.is_file() else path / CONTEST_FILE_NAME
        data = _load_json(config_path)
        names = data.get("problems")
        if not isinstance(names, list) or not names:
            raise ValueError(f"{config_path} must contain a non-empty problems list")
        directory = config_path.parent.resolve()
        problems: dict[str, ProblemPackage] = {}
        for name in names:
            if not isinstance(name, str) or not name or "/" in name or name == "..":
                raise ValueError(f"invalid problem name in {config_path}: {name!r}")
            problems[name] = ProblemPackage.load(directory / name)
        return cls(directory, problems)

    def problem(self, name: str) -> ProblemPackage:
        try:
            return self.problems[name]
        except KeyError:
            available = ", ".join(sorted(self.problems))
            raise KeyError(f"no problem {name!r} (have: {available})") from None


def load_target(path: Path | str) -> Contest:
    """Load whatever a path points at - a contest, or a single problem."""
    path = Path(path)
    if path.is_file():
        if path.name == CONTEST_FILE_NAME:
            return Contest.load(path)
        package = ProblemPackage.load(path)
        return Contest(package.directory.parent, {package.name: package})
    if (path / CONTEST_FILE_NAME).is_file():
        return Contest.load(path)
    package = ProblemPackage.load(path)
    return Contest(package.directory.parent, {package.name: package})
