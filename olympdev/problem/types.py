from pathlib import Path
from typing import Self
from enum import Enum
import shlex

from pydantic import model_validator

from olympdev.config import (
    MAX_ASPACE_LIMIT_KB,
    MAX_FILE_SIZE_LIMIT_KB,
    MAX_MEMORY_LIMIT_MB,
    MAX_TEST_FILE_BYTES,
    MAX_TESTS_PER_PROBLEM,
    MAX_TIME_LIMIT_MS,
    MAX_WALL_TIME_MS,
    check_limit_range,
)
from olympdev.schema import FrozenModel
from olympdev.worker.verdict import Verdict


def parse_gen_argv(gen_cmd: str) -> list[str]:
    """Split ``gen_cmd`` from info.json; drop the binary name, keep arguments."""
    parts = shlex.split(gen_cmd)
    if not parts:
        raise ValueError("empty gen_cmd")
    if parts[0].startswith("-"):
        raise ValueError("gen_cmd must start with a generator name")
    return parts[1:]


class ProblemTest(FrozenModel):
    input_file: str | None = None
    output_file: str | None = None
    gen_cmd: str | None = None
    name: str | None = None

    group_id: None = None  # reserved for future use, see TODO.md: Subtasks
    tags: None = None      # reserved for future use, see TODO.md: Subtasks

    @model_validator(mode="after")
    def _check_source(self) -> Self:
        if (self.input_file is None) == (self.gen_cmd is None):
            raise ValueError("exactly one of input_file or gen_cmd")
        if self.gen_cmd is not None:
            if not self.name:
                raise ValueError("generated tests require name")
            if self.output_file is not None:
                raise ValueError("generated tests cannot have output_file")
            parse_gen_argv(self.gen_cmd)
        return self

    def display_name(self) -> str:
        if self.name:
            return self.name
        assert self.input_file is not None
        return self.input_file


class ProblemSourceType(str, Enum):
    GEN = "gen"
    CHECK = "check"
    VALID = "valid"


class ProblemSource(FrozenModel):
    file: str
    type: ProblemSourceType


class ProblemSolution(FrozenModel):
    file: str
    tag: str  # main, OK, WA, TL, ML, RE, etc. see TODO.md: Solution tag logic
    group_tags: None = None  # reserved for future use, see TODO.md: Subtasks

    @model_validator(mode="after")
    def _check_tag(self) -> Self:
        # will by changed to logic validation later (see TODO.md: Solution tag logic)
        if self.tag not in ["main", "OK", "WA", "TL", "ML"]:
            raise ValueError(f"invalid solution tag: {self.tag!r}")
        return self

    def is_main_solution(self) -> bool:
        return self.tag == "main"

    # TODO: add logic validation later (see TODO.md: Solution tag logic)
    # Список разрешенных вердиктов для решения.
    def get_permissible_verdicts(self) -> list[Verdict]:
        res = [Verdict.OK]
        if self.tag == "WA":
            res.append(Verdict.WA)
        if self.tag == "TL":
            res.append(Verdict.TL)
        if self.tag == "ML":
            res.append(Verdict.ML)
        if self.tag == "RE":
            res.append(Verdict.RE)
        return res

    # TODO: add logic validation later (see TODO.md: Solution tag logic)
    # Один из этих вердиктов обязательно должен быть получен в ходе тестирования.
    def get_expected_verdicts(self) -> list[Verdict]:
        if self.tag == "WA":
            return [Verdict.WA]
        if self.tag == "TL":
            return [Verdict.TL]
        if self.tag == "ML":
            return [Verdict.ML]
        if self.tag == "RE":
            return [Verdict.RE]
        return []


class ProblemConfiguration(FrozenModel):
    time_limit: int
    memory_limit: int
    input_file_name: str
    output_file_name: str
    samples: list[ProblemTest]
    tests: list[ProblemTest]
    wall_limit: int | None = None
    aspace_limit: int | None = None
    file_size_limit: int | None = None
    use_io_redirect: bool = True

    solutions: list[ProblemSolution]
    sources: list[ProblemSource]

    @model_validator(mode="after")
    def _check_limits(self) -> Self:
        check_limit_range(
            "time_limit", self.time_limit, minimum=1, maximum=MAX_TIME_LIMIT_MS
        )
        check_limit_range(
            "memory_limit",
            self.memory_limit,
            minimum=1,
            maximum=MAX_MEMORY_LIMIT_MB,
        )
        if self.wall_limit is not None:
            check_limit_range(
                "wall_limit",
                self.wall_limit,
                minimum=1,
                maximum=MAX_WALL_TIME_MS,
            )
        if self.aspace_limit is not None:
            check_limit_range(
                "aspace_limit",
                self.aspace_limit,
                minimum=1,
                maximum=MAX_ASPACE_LIMIT_KB,
            )
        if self.file_size_limit is not None:
            check_limit_range(
                "file_size_limit",
                self.file_size_limit,
                minimum=1,
                maximum=MAX_FILE_SIZE_LIMIT_KB,
            )
        n_tests = len(self.samples) + len(self.tests)
        if n_tests > MAX_TESTS_PER_PROBLEM:
            raise ValueError(
                f"too many tests: {n_tests} (max {MAX_TESTS_PER_PROBLEM})"
            )
        return self

    def get_wall_limit(self) -> int:
        if self.wall_limit is None:
            return min(2 * self.time_limit, MAX_WALL_TIME_MS)
        return self.wall_limit

    def get_file_size_limit(self) -> int:
        if self.file_size_limit is None:
            return 16 * 1024
        return self.file_size_limit


def resolve_problem_file(problem_dir: Path, relative: str) -> Path:
    """Resolve a test path inside problem_dir. Rejects abs, .., and symlinks."""
    if not relative or Path(relative).is_absolute() or relative.startswith("/"):
        raise ValueError(f"test path must be relative: {relative!r}")
    parts = Path(relative).parts
    if ".." in parts or not parts:
        raise ValueError(f"test path must not contain '..': {relative!r}")
    root = problem_dir.resolve()
    candidate = root / relative
    if candidate.is_symlink():
        raise ValueError(f"test path must not be a symlink: {relative!r}")
    resolved = candidate.resolve()
    if not resolved.is_relative_to(root):
        raise ValueError(f"test path escapes problem dir: {relative!r}")
    if not resolved.is_file() or resolved.is_symlink():
        raise FileNotFoundError(relative)
    if resolved.stat().st_size > MAX_TEST_FILE_BYTES:
        raise ValueError(
            f"test file too large: {relative!r} ({resolved.stat().st_size} bytes)"
        )
    return resolved
