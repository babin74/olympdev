"""Creating a working problem or contest skeleton on disk.

Submitter side: this is the one place that writes a problem directory rather
than reading one. What it writes is a *working* problem - `olympdev verify`
passes on a freshly created skeleton - so an author starts by editing
something real instead of reconstructing the format from an example.

Templates are packaged assets (``olympdev/assets/templates``), read through
:mod:`olympdev.resources` like ``olymp.sty`` and ``testlib.h``.
"""

from __future__ import annotations

import json
from pathlib import Path

from olympdev.problem.package import CONTEST_FILE_NAME, INFO_FILE_NAME
from olympdev.resources import asset_path

PROBLEM_TEMPLATE_DIR = "templates/problem"
CONTEST_TEMPLATE_DIR = "templates/contest"

#: Replaced by the problem's name in the rendered info.json. A placeholder
#: rather than a jinja2 template: a skeleton must be readable as-is, and
#: statement rendering is the only thing here that earns a template engine.
_TITLE_PLACEHOLDER = "__TITLE__"


def _template_files(directory: str) -> list[tuple[str, bytes]]:
    """Every file under a packaged template directory, path-relative."""
    root = asset_path(directory)
    found: list[tuple[str, bytes]] = []

    def walk(node, prefix: str) -> None:
        for child in sorted(node.iterdir(), key=lambda item: item.name):
            relative = f"{prefix}{child.name}"
            if child.is_dir():
                walk(child, f"{relative}/")
            else:
                found.append((relative, child.read_bytes()))

    walk(root, "")
    return found


def _write_all(directory: Path, files: list[tuple[str, bytes]]) -> list[Path]:
    """Write every file, refusing to touch one that already exists.

    Checked up front, so a refusal leaves nothing half-written behind.
    """
    targets = [(directory / relative, content) for relative, content in files]
    existing = [path for path, _ in targets if path.exists()]
    if existing:
        names = ", ".join(str(path) for path in sorted(existing))
        raise FileExistsError(f"refusing to overwrite: {names}")
    written: list[Path] = []
    for path, content in targets:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        written.append(path)
    return written


def _titled(relative: str, content: bytes, title: str) -> bytes:
    if relative not in (INFO_FILE_NAME, CONTEST_FILE_NAME):
        return content
    return content.replace(_TITLE_PLACEHOLDER.encode(), title.encode())


def new_problem(directory: Path | str, *, title: str | None = None) -> list[Path]:
    """Create a problem skeleton in ``directory``. Returns the files written."""
    directory = Path(directory)
    name = directory.name
    if not name or name in (".", ".."):
        raise ValueError(f"not a usable problem directory name: {directory}")
    shown_title = title or name
    files = [
        (relative, _titled(relative, content, shown_title))
        for relative, content in _template_files(PROBLEM_TEMPLATE_DIR)
    ]
    return _write_all(directory, files)


def new_contest(
    directory: Path | str, *, problem: str, title: str | None = None
) -> list[Path]:
    """Create a contest skeleton holding one first problem.

    A problem is required because a contest lists its problems explicitly and
    an empty list is not a loadable contest - creating one would hand the
    author a file that fails on their next command.
    """
    directory = Path(directory)
    shown_title = title or directory.name
    written = _write_all(
        directory,
        [
            (relative, _titled(relative, content, shown_title))
            for relative, content in _template_files(CONTEST_TEMPLATE_DIR)
        ],
    )
    written.extend(new_problem(directory / problem))
    register_problem(directory / CONTEST_FILE_NAME, problem)
    return written


def register_problem(contest_file: Path, name: str) -> None:
    """Add a problem to a contest's list, keeping the file's own formatting.

    A contest lists its problems explicitly, so a problem created inside one
    that is not listed would silently not exist.
    """
    data = json.loads(contest_file.read_text(encoding="utf-8"))
    problems = data.get("problems")
    if not isinstance(problems, list):
        raise ValueError(f"{contest_file} has no problems list")
    if name in problems:
        return
    problems.append(name)
    contest_file.write_text(
        json.dumps(data, indent=4, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def contest_file_for(directory: Path) -> Path | None:
    """The contest a directory belongs to, if it is inside one."""
    candidate = directory.parent / CONTEST_FILE_NAME
    return candidate if candidate.is_file() else None
