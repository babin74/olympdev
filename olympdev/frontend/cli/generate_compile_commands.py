"""Generate compile_options.json so the IDE can resolve includes and dialect."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping

import olympdev
from olympdev.problem.package import resolve_target_config

_SOURCE_SUFFIXES = {".c", ".cc", ".cpp", ".cxx"}
_SOURCE_DIRS = ("source", "solutions", "files")
_COMPILE_FLAGS = ("-O2",)


def packaged_assets_dir() -> Path | None:
    assets = Path(olympdev.__file__).resolve().parent / "assets"
    if (assets / "testlib.h").is_file():
        return assets
    return None


def find_compiler(*, c_language: bool = False) -> str:
    names = ("gcc", "clang", "cc") if c_language else ("g++", "clang++", "c++")
    for name in names:
        path = shutil.which(name)
        if path is not None:
            return path
    return "/usr/bin/gcc" if c_language else "/usr/bin/g++"


def load_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit(f"configuration file not found: {path}") from None
    except json.JSONDecodeError as exc:
        raise SystemExit(f"invalid JSON in {path}: {exc}") from None
    if not isinstance(data, dict):
        raise SystemExit(f"expected a JSON object in {path}")
    return data


def is_contest_config(data: Mapping[str, Any]) -> bool:
    return "problems" in data


def compile_options_relpath(info: Mapping[str, Any]) -> str | None:
    dev = info.get("dev")
    if not isinstance(dev, dict):
        return None
    path = dev.get("compile_options_path")
    if not isinstance(path, str) or not path.strip():
        return None
    return path


def listed_solution_files(info: Mapping[str, Any]) -> Iterable[str]:
    solutions = info.get("solutions")
    if not isinstance(solutions, list):
        return []
    files: list[str] = []
    for item in solutions:
        if isinstance(item, dict) and isinstance(item.get("file"), str):
            files.append(item["file"])
    return files


def collect_sources(problem_dir: Path, info: Mapping[str, Any]) -> list[Path]:
    found: dict[Path, None] = {}
    for relative in listed_solution_files(info):
        candidate = problem_dir / relative
        if candidate.is_file() and candidate.suffix.lower() in _SOURCE_SUFFIXES:
            found[candidate.resolve()] = None
    for dirname in _SOURCE_DIRS:
        root = problem_dir / dirname
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if path.is_file() and path.suffix.lower() in _SOURCE_SUFFIXES:
                found[path.resolve()] = None
    return list(found)


def include_directories(problem_dir: Path) -> list[str]:
    dirs: list[str] = []
    assets = packaged_assets_dir()
    if assets is not None:
        dirs.append(str(assets))
    source_dir = problem_dir / "source"
    if source_dir.is_dir():
        dirs.append(str(source_dir.resolve()))
    return dirs


def compile_entry(source: Path, problem_dir: Path) -> dict[str, Any]:
    compiler = find_compiler(c_language=source.suffix.lower() == ".c")
    arguments: list[str] = [compiler, *_COMPILE_FLAGS]
    for include_dir in include_directories(problem_dir):
        arguments.extend(["-I", include_dir])
    arguments.extend(["-c", str(source)])
    return {
        "directory": str(problem_dir.resolve()),
        "file": str(source),
        "arguments": arguments,
    }


def problem_directories(config_path: Path, data: Mapping[str, Any]) -> list[Path]:
    if is_contest_config(data):
        problems = data["problems"]
        if not isinstance(problems, list) or not problems:
            raise SystemExit(f"{config_path} must contain a non-empty problems list")
        contest_dir = config_path.parent
        dirs: list[Path] = []
        for name in problems:
            if not isinstance(name, str) or not name:
                raise SystemExit(f"invalid problem name in {config_path}: {name!r}")
            dirs.append(contest_dir / name)
        return dirs
    return [config_path.parent]


def info_path_for(problem_dir: Path, config_path: Path) -> Path:
    if config_path.name == "info.json" and config_path.parent.resolve() == problem_dir.resolve():
        return config_path
    return problem_dir / "info.json"


def collect_outputs(target: Path) -> dict[Path, list[dict[str, Any]]]:
    # Same target grammar as every other command: a contest directory, an
    # olympdev.json, a problem directory or an info.json.
    config_path = resolve_target_config(target)
    data = load_json(config_path)
    grouped: dict[Path, list[dict[str, Any]]] = defaultdict(list)
    for problem_dir in problem_directories(config_path, data):
        info_path = info_path_for(problem_dir, config_path)
        info = load_json(info_path)
        relative = compile_options_relpath(info)
        if relative is None:
            print(
                f"skip {problem_dir}: no dev.compile_options_path",
                file=sys.stderr,
            )
            continue
        output = (problem_dir / relative).resolve()
        sources = collect_sources(problem_dir, info)
        if not sources:
            print(f"skip {problem_dir}: no C/C++ sources", file=sys.stderr)
            continue
        grouped[output].extend(
            compile_entry(source, problem_dir) for source in sources
        )
    if not grouped:
        raise SystemExit("no compile_options.json targets found")
    return grouped


def write_compile_options(path: Path, entries: list[dict[str, Any]]) -> None:
    unique: dict[str, dict[str, Any]] = {}
    for entry in entries:
        unique[entry["file"]] = entry
    ordered = [unique[key] for key in sorted(unique)]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(ordered, indent=4, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Generate compile_options.json (compilation database) from an "
            "olympdev.json contest config or a problem info.json."
        )
    )
    parser.add_argument(
        "config",
        type=Path,
        help="Path to olympdev.json or problem info.json",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    for output, entries in collect_outputs(args.config).items():
        write_compile_options(output, entries)
        print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
