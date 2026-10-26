"""Rendering a problem or contest statement into a compilable LaTeX source.

Submitter-side only - like ``problem/build.py``, this is never imported by
``worker/``. It turns a :class:`StatementSpec` (and, for a whole contest, a
:class:`ContestStatementConfig`) into plain values and bytes: a rendered
``.tex`` document plus a flat list of files to stage alongside it. Uploading
those to storage and picking the compile command is ``build.py``'s job, the
same split ``compile_spec`` already draws for compiling programs.

Samples are auto-inserted from the same test files the testing pipeline
judges against, staged as raw files and pulled in with olymp.sty's
``\\exmpfile`` (which typesets a file's contents verbatim at compile time) -
so a statement never has to re-encode or LaTeX-escape sample text by hand,
and can never show something different from what is actually checked.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from olympdev.problem.package import ProblemPackage
from olympdev.problem.types import (
    ContestStatementConfig,
    ProblemGroup,
    ProblemTest,
    StatementSpec,
)
from olympdev.resources import read_asset_text

PROBLEM_TEMPLATE_ASSET = "statement/problem.tex.jinja2"
CONTEST_TEMPLATE_ASSET = "statement/contest.tex.jinja2"
OLYMP_STY_ASSET = "olymp.sty"


@dataclass(frozen=True)
class SampleEntry:
    """One statement sample.

    ``output_path`` is ``None`` when the problem has no static answer on disk
    for this sample - the usual case, since the testing pipeline already gets
    it from the model solution instead (see ``ProblemPackage.role_files``).
    The statement pipeline computes it the same way - see
    ``pending_sample_tests``/``build_problem_statement_input`` - rather than
    requiring every sample to carry a hand-written ``output_file``.
    """

    stem: str
    input_path: Path
    output_path: Path | None
    #: The original info.json test, kept so a pending sample can be re-issued
    #: as a synthetic test (same input_file, name=stem) when computing its
    #: answer - see ``pending_sample_tests``.
    source_test: ProblemTest


SampleFiles = list[SampleEntry]


def _jinja_environment():
    """A jinja2 Environment using LaTeX-friendly delimiters.

    Jinja2's default ``{{ }}``/``{% %}`` collide with LaTeX's own braces, so
    this follows the standard approach for templating LaTeX with jinja2:
    ``\\VAR{...}`` for variables, ``\\BLOCK{...}`` for control flow.
    """
    import jinja2

    return jinja2.Environment(
        block_start_string=r"\BLOCK{",
        block_end_string="}",
        variable_start_string=r"\VAR{",
        variable_end_string="}",
        comment_start_string=r"\#{",
        comment_end_string="}",
        trim_blocks=True,
        lstrip_blocks=True,
        autoescape=False,
    )


def render_template(template_text: str, context: dict[str, Any]) -> str:
    return _jinja_environment().from_string(template_text).render(**context)


def read_samples(package: ProblemPackage, prefix: str = "") -> SampleFiles:
    """Each sample's input file, plus its output file when one is on disk.

    ``prefix`` goes in front of every stem: a contest booklet stages all its
    problems' samples into one sandbox, so each problem needs its own.

    Statement building needs a real input file to hand ``\\exmpfile``; a
    sample produced by a generator (``gen_cmd``, no ``input_file`` on disk)
    has nothing to stage, so it is rejected here rather than silently
    omitted. The output file is optional - see ``pending_sample_tests``.
    """
    samples: SampleFiles = []
    for index, test in enumerate(package.configuration.samples):
        if test.input_file is None:
            raise ValueError(
                f"{package.name}: statement samples must have an input file "
                "on disk (generated samples are not supported for "
                f"statements): {test.display_name()}"
            )
        samples.append(
            SampleEntry(
                stem=f"{prefix}sample-{index}",
                input_path=package.file(test.input_file),
                output_path=(
                    package.file(test.output_file) if test.output_file is not None else None
                ),
                source_test=test,
            )
        )
    return samples


def sample_sources(samples: SampleFiles) -> list[tuple[Path, str]]:
    """(path, sandbox name) pairs for every sample's input, plus any output
    already on disk. A sample without one needs its answer computed by
    running the model/main solution - see ``pending_sample_tests``."""
    sources: list[tuple[Path, str]] = []
    for sample in samples:
        sources.append((sample.input_path, f"{sample.stem}.in"))
        if sample.output_path is not None:
            sources.append((sample.output_path, f"{sample.stem}.out"))
    return sources


def pending_sample_tests(samples: SampleFiles) -> list[tuple[str, ProblemTest]]:
    """(stem, source test) for every sample whose answer must be computed.

    The source test is re-issued as a synthetic test (same ``input_file``,
    ``name=stem``) against the problem's own model/main solution - see
    ``build_problem_statement_input``.
    """
    return [
        (sample.stem, sample.source_test) for sample in samples if sample.output_path is None
    ]


def stage_asset_dir(root: Path) -> list[tuple[Path, str]]:
    """Every file under an already-resolved asset directory, as (path, sandbox name)."""
    return [
        (path, str(path.relative_to(root)))
        for path in sorted(root.rglob("*"))
        if path.is_file()
    ]


#: olymp.sty's \SubtaskWithCost{<id>}{<points>} renders "Subtask <id>
#: (<points> points)" - confirmed from the bundled olymp.sty's own macro
#: definition (\createsectionpartwo{\SubtaskWithCost}{\kw@Subtask}{\kw@points},
#: expanding to "#2 ##1 (##2 #3)" - id first, then points). There is no
#: olymp.sty macro for the depends_on relationship, so it is spelled out as
#: one localized sentence per group instead.
_DEPENDS_ON_SENTENCE = {
    "ru": "Тесты этой группы также удовлетворяют ограничениям групп {ids}.",
    "en": "Tests in this group also satisfy the constraints of group(s) {ids}.",
}


def render_subtask_table(groups: list[ProblemGroup], *, lang: str) -> str:
    """The problem's group/points table, as body text following \\Constraints.

    Empty for a problem with no groups - a problem without ``mode``/``groups``
    in info.json renders its statement exactly as before this existed.
    ``ProblemGroup`` has no free-text "constraints" field (see TODO.md:
    Subtasks); ``group.name`` doubles as that summary here.
    """
    if not groups:
        return ""
    sentence = _DEPENDS_ON_SENTENCE.get(lang, _DEPENDS_ON_SENTENCE["en"])
    blocks: list[str] = []
    for group in sorted(groups, key=lambda item: item.id):
        lines = [f"\\SubtaskWithCost{{{group.id}}}{{{group.points}}}"]
        if group.name:
            lines.append(group.name)
        if group.depends_on:
            ids = ", ".join(str(dep) for dep in sorted(group.depends_on))
            lines.append(sentence.format(ids=ids))
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def render_problem_fragment(
    *,
    spec: StatementSpec,
    time_limit_ms: int,
    memory_limit_mb: int,
    body: str,
    samples: SampleFiles,
    groups: list[ProblemGroup] = [],
    template_text: str | None = None,
) -> str:
    """One problem's ``\\begin{problem}...\\end{problem}`` fragment."""
    text = template_text or read_asset_text(PROBLEM_TEMPLATE_ASSET)
    seconds = time_limit_ms / 1000
    if seconds == int(seconds):
        seconds = int(seconds)
    return render_template(
        text,
        {
            "title": spec.title,
            "time_limit": f"{seconds} second{'s' if seconds != 1 else ''}",
            "memory_limit": f"{memory_limit_mb} MB",
            "body": body,
            "samples": [{"stem": sample.stem} for sample in samples],
            "subtasks_tex": render_subtask_table(groups, lang=spec.lang),
        },
    )


#: olymp.sty's localization is a package *option* (\\usepackage[russian]{olymp}),
#: not something babel's active language implies - see rules.md §8. "ru" is
#: the only lang that turns it on; anything else renders with English keywords.
_OLYMP_RUSSIAN_LANG = "ru"
_BABEL_LANGUAGE = {"ru": "russian"}
_BABEL_DEFAULT_LANGUAGE = "english"


def render_contest_document(
    *,
    contest: ContestStatementConfig,
    fragments: list[str],
    template_text: str | None = None,
    lang: str,
) -> str:
    """The full document: preamble, ``\\contest{...}``, then every fragment.

    ``lang`` picks olymp.sty's ``russian`` package option and babel's active
    language for the whole document - a contest-wide build already renders
    every problem's fragment under one shared ``lang`` (see
    ``build_contest_statement_input``), so there is no per-fragment language
    to reconcile here.
    """
    text = template_text or read_asset_text(CONTEST_TEMPLATE_ASSET)
    return render_template(
        text,
        {
            "title": contest.title,
            "location": contest.location,
            "date": contest.date,
            "fragments": fragments,
            "olymp_options": "russian" if lang == _OLYMP_RUSSIAN_LANG else "",
            "babel_language": _BABEL_LANGUAGE.get(lang, _BABEL_DEFAULT_LANGUAGE),
        },
    )
