"""The olympdev command.

Every subcommand builds a view model and prints it through
``olympdev.frontend.text``; ``--html PATH`` writes the same view through the
HTML renderer. The CLI, a notebook and any future web UI therefore show the
same facts rendered by the same code.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from olympdev.frontend import text as text_view
from olympdev.frontend.notebook import Workbench
from olympdev.frontend.view import PipelineRunView, VerificationReportView
from olympdev.settings import Settings, get_settings, load_settings, set_settings
from olympdev.storage import open_storage


def _write_html(view: Any, path: Path) -> None:
    from olympdev.frontend import html as html_view

    if isinstance(view, PipelineRunView):
        markup = html_view.render_pipeline_run(view)
    elif isinstance(view, VerificationReportView):
        markup = html_view.render_verification_report(view)
    else:  # pragma: no cover - guarded by the subcommands
        raise TypeError(f"no HTML renderer for {type(view).__name__}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(markup, encoding="utf-8")
    print(f"wrote {path}", file=sys.stderr)


def _emit(view: Any, args: argparse.Namespace, rendered: str) -> None:
    print(rendered)
    if args.html is not None:
        _write_html(view, args.html)


def _workbench(args: argparse.Namespace) -> Workbench:
    return Workbench.open(args.target, jobs=args.jobs)


def cmd_run(args: argparse.Namespace) -> int:
    workbench = _workbench(args)
    names = [args.problem] if args.problem else workbench.problems()
    failed = False
    for name in names:
        view = workbench.problem(name).run(args.solution)
        _emit(view, args, text_view.render_pipeline_run(view))
        failed = failed or (view.verdict is None or view.verdict.value != "OK")
    return 1 if failed and args.strict else 0


def cmd_verify(args: argparse.Namespace) -> int:
    workbench = _workbench(args)
    names = [args.problem] if args.problem else workbench.problems()
    failed = False
    for name in names:
        view = workbench.problem(name).verify(args.solution)
        _emit(view, args, text_view.render_verification_report(view))
        failed = failed or not view.matched
    return 1 if failed else 0


def cmd_statement(args: argparse.Namespace) -> int:
    workbench = _workbench(args)
    names = [args.problem] if args.problem else workbench.problems()
    if args.out is not None and len(names) > 1:
        print("error: --out needs a single problem (pass --problem)", file=sys.stderr)
        return 2
    failed = False
    for name in names:
        view = workbench.problem(name).statement(args.lang)
        print(text_view.render_statement_result(view))
        if view.pdf_digest is None:
            failed = True
            continue
        if args.out is not None:
            workbench.storage.download_file(view.pdf_digest, args.out)
            print(f"wrote {args.out}", file=sys.stderr)
    return 1 if failed else 0


def cmd_stress(args: argparse.Namespace) -> int:
    workbench = _workbench(args)
    names = [args.problem] if args.problem else workbench.problems()
    found_any = False
    for name in names:
        view = workbench.problem(name).stress(
            args.candidate,
            against=args.against,
            group=args.group,
            gen_args=args.gen_args,
            iterations=args.iterations,
        )
        print(text_view.render_stress_result(view))
        found_any = found_any or view.found
    return 1 if found_any else 0


def cmd_compile_commands(args: argparse.Namespace) -> int:
    from olympdev.frontend.cli.generate_compile_commands import (
        collect_outputs,
        write_compile_options,
    )

    for output, entries in collect_outputs(args.target).items():
        write_compile_options(output, entries)
        print(output)
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    storage = open_storage(get_settings())
    content = storage.download_bytes(args.digest)
    if content is None:
        print(f"no blob {args.digest}", file=sys.stderr)
        return 1
    try:
        parsed = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError):
        sys.stdout.buffer.write(content)
        return 0
    print(json.dumps(parsed, indent=2, ensure_ascii=False, sort_keys=True))
    return 0


def cmd_config(args: argparse.Namespace) -> int:
    settings = get_settings()
    source = settings.source_path or "built-in defaults"
    print(f"source: {source}")
    print(json.dumps(settings.model_dump(mode="json"), indent=2, sort_keys=True))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="olympdev", description="Contest problem tooling."
    )
    parser.add_argument(
        "--config", type=Path, help="settings file to use instead of the search chain"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_target(sub: argparse.ArgumentParser) -> None:
        sub.add_argument(
            "target",
            type=Path,
            nargs="?",
            default=Path.cwd(),
            help="contest directory, olympdev.json, problem directory or info.json",
        )
        sub.add_argument("--problem", help="problem name inside a contest")
        sub.add_argument("--jobs", type=int, default=None, help="parallel sandboxes")
        sub.add_argument("--html", type=Path, help="also write the report as HTML")

    run = subparsers.add_parser("run", help="test a solution against a problem")
    add_target(run)
    run.add_argument(
        "--solution", default=None, help="solution name, its file's stem (default: main)"
    )
    run.add_argument(
        "--strict", action="store_true", help="exit non-zero unless the verdict is OK"
    )
    run.set_defaults(func=cmd_run)

    verify = subparsers.add_parser(
        "verify", help="check prepared solutions against their tags"
    )
    add_target(verify)
    verify.add_argument(
        "--solution",
        default=None,
        help="verify one solution by name (default: every non-ignored solution)",
    )
    verify.set_defaults(func=cmd_verify)

    statement = subparsers.add_parser(
        "statement", help="compile a problem's statement into a PDF"
    )
    statement.add_argument(
        "target",
        type=Path,
        nargs="?",
        default=Path.cwd(),
        help="contest directory, olympdev.json, problem directory or info.json",
    )
    statement.add_argument("--problem", help="problem name inside a contest")
    statement.add_argument("--jobs", type=int, default=None, help="parallel sandboxes")
    statement.add_argument("--lang", required=True, help="statement language, e.g. ru")
    statement.add_argument("--out", type=Path, help="also save the compiled PDF here")
    statement.set_defaults(func=cmd_statement, html=None)

    stress = subparsers.add_parser(
        "stress", help="stress a candidate solution against a reference on growing seeds"
    )
    stress.add_argument(
        "target",
        type=Path,
        nargs="?",
        default=Path.cwd(),
        help="contest directory, olympdev.json, problem directory or info.json",
    )
    stress.add_argument("--problem", help="problem name inside a contest")
    stress.add_argument("--jobs", type=int, default=None, help="parallel sandboxes")
    stress.add_argument("--candidate", required=True, help="solution to stress (name or path)")
    stress.add_argument(
        "--against", default="model", help="reference solution (default: main solution)"
    )
    group_or_gen = stress.add_mutually_exclusive_group(required=True)
    group_or_gen.add_argument(
        "--group", type=int, help="reuse a generated test's own argv for this group"
    )
    group_or_gen.add_argument("--gen-args", help="literal generator argv, e.g. '-c=1000'")
    stress.add_argument(
        "--iterations", type=int, default=1000, help="iteration budget (default: 1000)"
    )
    stress.set_defaults(func=cmd_stress, html=None)

    compile_commands = subparsers.add_parser(
        "compile-commands", help="generate a compilation database for an IDE"
    )
    compile_commands.add_argument("target", type=Path)
    compile_commands.set_defaults(func=cmd_compile_commands, html=None)

    show = subparsers.add_parser("show", help="print a blob from storage by digest")
    show.add_argument("digest")
    show.set_defaults(func=cmd_show, html=None)

    config = subparsers.add_parser(
        "config", help="print the effective settings and where they came from"
    )
    config.set_defaults(func=cmd_config, html=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.config is not None:
        set_settings(load_settings(args.config))
    if getattr(args, "jobs", None) is None and args.command in {
        "run", "verify", "statement", "stress",
    }:
        args.jobs = get_settings().isolate.sandbox_count
    try:
        return args.func(args)
    except (FileNotFoundError, KeyError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
