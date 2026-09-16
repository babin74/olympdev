from pathlib import Path
from typing import Self
from enum import Enum
import shlex

from pydantic import model_validator

from olympdev.schema import FrozenModel
from olympdev.settings import StatementEngine, check_limit_range, get_settings
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


def parse_valid_argv(valid_cmd: str) -> list[str]:
    """Split ``valid_cmd`` from info.json into argv for the validator binary.

    Unlike ``gen_cmd``, there is no leading binary name to drop here - the
    validator's binary is already given separately (``program_digest``), so
    every token is an argument (e.g. testlib's own ``--group 1``, which
    ``registerValidation(argc, argv)`` already parses into ``validator.group()``).
    """
    parts = shlex.split(valid_cmd)
    if not parts:
        raise ValueError("empty valid_cmd")
    return parts


class ProblemTest(FrozenModel):
    input_file: str | None = None
    output_file: str | None = None
    gen_cmd: str | None = None
    name: str | None = None

    #: Which group this test scores into, when the problem's ``mode`` is
    #: ``"group-points"``. A test belongs to exactly one group - see
    #: TODO.md: Subtasks for why there is no multi-group membership.
    group_id: int | None = None
    tags: None = None  # reserved for future use, see TODO.md: Subtasks

    #: Extra argv for the validator on this test, e.g. ``"--group 1"``.
    #: Defaults (when mode is "group-points") to ``--group <group_id>`` - see
    #: ProblemTestingPipeline._resolve_valid_argv.
    valid_cmd: str | None = None

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

    @model_validator(mode="after")
    def _check_valid_cmd(self) -> Self:
        if self.valid_cmd is not None:
            parse_valid_argv(self.valid_cmd)
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
    #: Per-group expectation, when the problem's ``mode`` is
    #: ``"group-points"`` - group id (as a string, JSON keys are strings) to
    #: a tag in the same grammar as ``tag`` above, checked against only that
    #: group's tests. A group missing from here still defaults to requiring
    #: ``OK`` once ``group_tags`` is set at all. See TODO.md: Subtasks.
    group_tags: dict[str, str] | None = None

    @model_validator(mode="after")
    def _check_tag(self) -> Self:
        parse_solution_tag(self.tag)
        if self.group_tags is not None:
            for group_key, group_tag in self.group_tags.items():
                if not group_key.lstrip("-").isdigit():
                    raise ValueError(f"group_tags key {group_key!r} is not an integer")
                parse_solution_tag(group_tag)
        return self

    def group_tag_expectation(self, group_id: int) -> SolutionTagExpectation:
        """This solution's expectation for one group.

        Falls back to ``OK`` when ``group_tags`` is set but says nothing
        about this particular group - a declared expectation is a claim
        about every group, not just the ones mentioned.
        """
        if self.group_tags is None:
            raise ValueError(f"{self.file}: no group_tags declared")
        tag = self.group_tags.get(str(group_id), "OK")
        return parse_solution_tag(tag)

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


class ProblemScoringMode(str, Enum):
    """How a problem is scored. See TODO.md: Subtasks.

    Opt-in and explicit on purpose: a problem's ``tests``/``groups`` do not
    imply scoring by themselves, because the package already has a second,
    unrelated way to grant partial credit - a testlib checker exiting 7
    (``Verdict.PT``, per test). Naming the mode keeps the two from silently
    combining on one problem.
    """

    GROUP_POINTS = "group-points"


class ProblemGroup(FrozenModel):
    """One scored group (subtask) of tests, see TODO.md: Subtasks.

    ``depends_on`` is about test data, not scoring: it documents that this
    group's tests also satisfy the constraints of the groups it depends on
    (rules.md §3 in roi-rounds). It does not zero this group's points when a
    dependency fails - points are always recorded on the group itself.
    """

    id: int
    points: int
    name: str = ""
    depends_on: list[int] = []


class StatementSpec(FrozenModel):
    """One problem's statement in one language, from info.json."""

    lang: str
    engine: StatementEngine = StatementEngine.LATEX
    source: str
    title: str
    #: Overrides the built-in per-problem fragment template, resolved in the
    #: problem dir. None uses olympdev's bundled default.
    template: str | None = None
    #: Directories (relative to the problem dir) staged into the sandbox
    #: alongside the compiled source - for images, extra .sty files, etc.
    assets: list[str] = []


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
    statements: list[StatementSpec] = []
    dev: ProblemDevSettings = ProblemDevSettings()

    #: See ProblemScoringMode - unset means today's plain pass/fail behaviour,
    #: no groups, no score. See TODO.md: Subtasks.
    mode: ProblemScoringMode | None = None
    groups: list[ProblemGroup] = []

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
        self._check_statement_langs()
        self._check_groups()
        return self

    def _check_groups(self) -> None:
        """`groups`/`mode` are opt-in together; see ProblemScoringMode."""
        if not self.groups:
            if self.mode is not None:
                raise ValueError(
                    f"mode {self.mode.value!r} needs a non-empty groups list"
                )
            return
        if self.mode is None:
            raise ValueError(
                "groups needs an explicit mode (e.g. 'group-points')"
            )
        by_id: dict[int, ProblemGroup] = {}
        for group in self.groups:
            if group.id in by_id:
                raise ValueError(f"duplicate group id {group.id}")
            by_id[group.id] = group
        for group in self.groups:
            if group.id in group.depends_on:
                raise ValueError(f"group {group.id} depends on itself")
            for dep in group.depends_on:
                if dep not in by_id:
                    raise ValueError(
                        f"group {group.id} depends on unknown group {dep}"
                    )
        if self.mode is ProblemScoringMode.GROUP_POINTS:
            total_points = sum(group.points for group in self.groups)
            if total_points != 100:
                raise ValueError(
                    f"group points must sum to 100, got {total_points}"
                )
            for test in self.tests:
                if test.group_id is None:
                    raise ValueError(
                        f"{test.display_name()}: group_id is required "
                        f"when mode is {self.mode.value!r}"
                    )
                if test.group_id not in by_id:
                    raise ValueError(
                        f"{test.display_name()}: unknown group_id {test.group_id}"
                    )
            for solution in self.solutions:
                if solution.group_tags is None:
                    continue
                for group_key in solution.group_tags:
                    if int(group_key) not in by_id:
                        raise ValueError(
                            f"{solution.file}: group_tags references "
                            f"unknown group {group_key}"
                        )

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

    def _check_statement_langs(self) -> None:
        seen: dict[str, str] = {}
        for statement in self.statements:
            if statement.lang in seen:
                raise ValueError(
                    f"duplicate statement lang {statement.lang!r}: "
                    f"{seen[statement.lang]} and {statement.source}"
                )
            seen[statement.lang] = statement.source

    def statement_by_lang(self, lang: str) -> StatementSpec | None:
        for statement in self.statements:
            if statement.lang == lang:
                return statement
        return None

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


class ContestStatementConfig(FrozenModel):
    """How a contest's statement booklet looks, from olympdev.json."""

    title: str
    location: str = ""
    date: str
    #: Overrides the built-in contest document template, resolved in the
    #: contest dir. None uses olympdev's bundled default.
    template: str | None = None
    #: Directories (relative to the contest dir) staged into the sandbox -
    #: for a sponsor logo, shared branding, etc.
    assets: list[str] = []


def _resolve_problem_path(problem_dir: Path, relative: str) -> Path:
    """Resolve a path inside problem_dir. Rejects abs, .., and symlinks."""
    if not relative or Path(relative).is_absolute() or relative.startswith("/"):
        raise ValueError(f"path must be relative: {relative!r}")
    parts = Path(relative).parts
    if ".." in parts or not parts:
        raise ValueError(f"path must not contain '..': {relative!r}")
    root = problem_dir.resolve()
    candidate = root / relative
    if candidate.is_symlink():
        raise ValueError(f"path must not be a symlink: {relative!r}")
    resolved = candidate.resolve()
    if not resolved.is_relative_to(root):
        raise ValueError(f"path escapes problem dir: {relative!r}")
    return resolved


def resolve_problem_file(problem_dir: Path, relative: str) -> Path:
    """Resolve a test path inside problem_dir. Rejects abs, .., and symlinks."""
    resolved = _resolve_problem_path(problem_dir, relative)
    if not resolved.is_file() or resolved.is_symlink():
        raise FileNotFoundError(relative)
    if resolved.stat().st_size > get_settings().limits.max_test_file_bytes:
        raise ValueError(
            f"test file too large: {relative!r} ({resolved.stat().st_size} bytes)"
        )
    return resolved


def resolve_problem_dir(problem_dir: Path, relative: str) -> Path:
    """Resolve a directory inside problem_dir. Rejects abs, .., and symlinks."""
    resolved = _resolve_problem_path(problem_dir, relative)
    if not resolved.is_dir() or resolved.is_symlink():
        raise FileNotFoundError(relative)
    return resolved
