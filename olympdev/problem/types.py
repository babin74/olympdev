from pathlib import Path
from typing import Self
from enum import Enum
import shlex

from pydantic import model_validator

from olympdev.schema import FrozenModel
from olympdev.settings import check_limit_range, get_settings
from olympdev.worker.verdict import Verdict

#: Solution was tested successfully and is not OK. CE/FL/CF are not these.
PREDICTABLE_ERRORS = frozenset(
    {Verdict.WA, Verdict.TL, Verdict.ML, Verdict.RE, Verdict.PT}
)

_VERDICT_BY_CODE = {verdict.value: verdict for verdict in Verdict}
_PERMISSIBLE_ORDER = (
    Verdict.OK,
    Verdict.WA,
    Verdict.TL,
    Verdict.ML,
    Verdict.RE,
    Verdict.PT,
    Verdict.CE,
    Verdict.FL,
    Verdict.CF,
)


def _ordered_verdicts(verdicts: set[Verdict]) -> tuple[Verdict, ...]:
    return tuple(verdict for verdict in _PERMISSIBLE_ORDER if verdict in verdicts)


class SolutionTagExpectation(FrozenModel):
    """What a solution tag claims about the verdicts it may and must produce."""

    permissible: tuple[Verdict, ...] = ()
    required: tuple[Verdict, ...] = ()
    require_any_predictable: bool = False
    ignored: bool = False


def parse_solution_tag(tag: str) -> SolutionTagExpectation:
    """Parse a solution tag from info.json into expected and permitted verdicts."""
    if tag == "ignore":
        return SolutionTagExpectation(ignored=True)
    if tag in ("main", "OK"):
        return SolutionTagExpectation(permissible=(Verdict.OK,))
    if tag == "incor":
        return SolutionTagExpectation(
            permissible=_ordered_verdicts({Verdict.OK, *PREDICTABLE_ERRORS}),
            require_any_predictable=True,
        )
    parts = tag.split("+")
    if not tag or any(not part for part in parts):
        raise ValueError(f"invalid solution tag: {tag!r}")
    permissible: set[Verdict] = {Verdict.OK}
    required: list[Verdict] = []
    seen: set[Verdict] = set()
    for part in parts:
        optional = part.endswith("?")
        code = part[:-1] if optional else part
        verdict = _VERDICT_BY_CODE.get(code)
        if verdict is None:
            raise ValueError(f"invalid solution tag: {tag!r}")
        if verdict in seen:
            raise ValueError(f"duplicate token {code!r} in solution tag: {tag!r}")
        if not optional and verdict not in PREDICTABLE_ERRORS:
            raise ValueError(f"invalid solution tag: {tag!r}")
        seen.add(verdict)
        permissible.add(verdict)
        if not optional:
            required.append(verdict)
    return SolutionTagExpectation(
        permissible=_ordered_verdicts(permissible),
        required=tuple(required),
    )


def match_solution_tag(
    expectation: SolutionTagExpectation, observed: list[Verdict]
) -> str | None:
    """Return a mismatch reason, or None if the observed verdicts fit the tag."""
    if expectation.ignored:
        return None
    for verdict in observed:
        if verdict not in expectation.permissible:
            return f"unexpected verdict {verdict.value}"
    seen = set(observed)
    missing = [verdict for verdict in expectation.required if verdict not in seen]
    if missing:
        names = ", ".join(verdict.value for verdict in missing)
        return f"missing required verdict {names}"
    if expectation.require_any_predictable and not (seen & PREDICTABLE_ERRORS):
        return "expected a predictable error (WA, TL, ML, RE or PT)"
    return None


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
        parse_solution_tag(self.tag)
        return self

    @property
    def name(self) -> str:
        """The solution's identity for addressing - its file's stem.

        The tag is a verdict expectation (``WA``, ``TL+ML?``, ...), not a
        name a solution was given, so it is never used to address one.
        """
        return Path(self.file).stem

    def is_main_solution(self) -> bool:
        return self.tag == "main"

    def is_ignored(self) -> bool:
        return self.tag_expectation().ignored

    def tag_expectation(self) -> SolutionTagExpectation:
        return parse_solution_tag(self.tag)

    def get_permissible_verdicts(self) -> list[Verdict]:
        """Verdicts this solution may produce on a test."""
        return list(self.tag_expectation().permissible)

    def get_expected_verdicts(self) -> list[Verdict]:
        """Verdicts that must each appear at least once. Empty if none are required.

        ``incor`` is different: nothing is individually required, but
        ``tag_expectation().require_any_predictable`` demands some predictable
        error. Optional tokens (``TL?``) never appear here.
        """
        return list(self.tag_expectation().required)


class ProblemDevSettings(FrozenModel):
    """Editor conveniences from info.json; never affects testing."""

    compile_options_path: str | None = None


class ProblemConfiguration(FrozenModel):
    """A problem's info.json.

    Field names match the file, so info.json loads without a translation
    layer. Limits carry their unit in the name because they are read by
    people writing problems, not just by code.
    """

    time_limit_ms: int
    memory_limit_mb: int
    samples: list[ProblemTest] = []
    tests: list[ProblemTest] = []
    wall_limit_ms: int | None = None
    aspace_limit_kb: int | None = None
    file_size_limit_kb: int | None = None

    solutions: list[ProblemSolution] = []
    sources: list[ProblemSource] = []
    dev: ProblemDevSettings = ProblemDevSettings()

    @model_validator(mode="after")
    def _check_limits(self) -> Self:
        caps = get_settings().limits
        check_limit_range(
            "time_limit_ms", self.time_limit_ms, minimum=1, maximum=caps.max_time_limit_ms
        )
        check_limit_range(
            "memory_limit_mb",
            self.memory_limit_mb,
            minimum=1,
            maximum=caps.max_memory_limit_mb,
        )
        optional = (
            ("wall_limit_ms", self.wall_limit_ms, caps.max_wall_time_ms),
            ("aspace_limit_kb", self.aspace_limit_kb, caps.max_aspace_limit_kb),
            (
                "file_size_limit_kb",
                self.file_size_limit_kb,
                caps.max_file_size_limit_kb,
            ),
        )
        for name, value, maximum in optional:
            if value is not None:
                check_limit_range(name, value, minimum=1, maximum=maximum)
        n_tests = len(self.samples) + len(self.tests)
        if n_tests > caps.max_tests_per_problem:
            raise ValueError(
                f"too many tests: {n_tests} (max {caps.max_tests_per_problem})"
            )
        self._check_solution_tags()
        self._check_solution_names()
        self._check_sources()
        return self

    def _check_solution_tags(self) -> None:
        """One 'main' solution, and no tag used twice."""
        seen: dict[str, str] = {}
        for solution in self.solutions:
            if solution.tag in seen:
                raise ValueError(
                    f"duplicate solution tag {solution.tag!r}: "
                    f"{seen[solution.tag]} and {solution.file}"
                )
            seen[solution.tag] = solution.file
        if self.solutions and "main" not in seen:
            raise ValueError("a problem with solutions needs one tagged 'main'")

    def _check_solution_names(self) -> None:
        """A solution is addressed by its file's stem, so those must be unique."""
        seen: dict[str, str] = {}
        for solution in self.solutions:
            if solution.name in seen:
                raise ValueError(
                    f"duplicate solution name {solution.name!r}: "
                    f"{seen[solution.name]} and {solution.file}"
                )
            seen[solution.name] = solution.file

    def _check_sources(self) -> None:
        seen: dict[ProblemSourceType, str] = {}
        for source in self.sources:
            if source.type in seen:
                raise ValueError(
                    f"duplicate {source.type.value} source: "
                    f"{seen[source.type]} and {source.file}"
                )
            seen[source.type] = source.file

    def get_wall_limit(self) -> int:
        if self.wall_limit_ms is None:
            return min(2 * self.time_limit_ms, get_settings().limits.max_wall_time_ms)
        return self.wall_limit_ms

    def get_file_size_limit(self) -> int:
        if self.file_size_limit_kb is None:
            return 16 * 1024
        return self.file_size_limit_kb

    def source(self, source_type: ProblemSourceType) -> ProblemSource | None:
        for source in self.sources:
            if source.type is source_type:
                return source
        return None

    def solution_by_name(self, name: str) -> ProblemSolution | None:
        for solution in self.solutions:
            if solution.name == name:
                return solution
        return None

    def main_solution(self) -> ProblemSolution | None:
        for solution in self.solutions:
            if solution.tag == "main":
                return solution
        return None

    def needs_model_solution(self) -> bool:
        """True when some test has no answer file, so one must be generated."""
        return any(
            test.output_file is None for test in (*self.samples, *self.tests)
        )


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
    if resolved.stat().st_size > get_settings().limits.max_test_file_bytes:
        raise ValueError(
            f"test file too large: {relative!r} ({resolved.stat().st_size} bytes)"
        )
    return resolved
