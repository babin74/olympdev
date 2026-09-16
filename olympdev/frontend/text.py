"""Terminal rendering of the same view models the HTML templates use."""

from __future__ import annotations

from olympdev.frontend.view import (
    PipelineRunView,
    StatementResultView,
    StressResultView,
    VerificationReportView,
)


def _table(rows: list[list[str]], *, header: list[str]) -> str:
    columns = list(zip(*([header] + rows))) if rows else [[h] for h in header]
    widths = [max(len(cell) for cell in column) for column in columns]

    def line(cells: list[str]) -> str:
        return "  ".join(cell.ljust(width) for cell, width in zip(cells, widths)).rstrip()

    out = [line(header), line(["-" * width for width in widths])]
    out.extend(line(row) for row in rows)
    return "\n".join(out)


def render_pipeline_run(view: PipelineRunView) -> str:
    head = f"{view.problem_name or view.pipeline_id}: {view.verdict_text}"
    if view.wall_seconds is not None:
        head += f"  (cpu/wall {view.time_text})"
    out = [head]
    if view.failure_reason:
        out.append(f"  failed: {view.failure_reason}")
    if view.tests:
        out.append(
            _table(
                [
                    [
                        test.name,
                        test.verdict.value,
                        test.usage.time_text,
                        test.usage.memory_text,
                    ]
                    for test in view.tests
                ],
                header=["test", "verdict", "cpu/wall", "memory"],
            )
        )
    return "\n".join(out)


def render_statement_result(view: StatementResultView) -> str:
    head = f"{view.problem_name or 'statement'} ({view.lang}): {view.status.value}"
    if view.wall_seconds is not None:
        head += f"  (wall {view.wall_seconds:.2f}s)"
    out = [head]
    if view.failure_reason:
        out.append(f"  failed: {view.failure_reason}")
    if view.pdf_digest is not None:
        out.append(f"  pdf {view.pdf_digest}")
    return "\n".join(out)


def render_stress_result(view: StressResultView) -> str:
    head = f"{view.problem_name or 'stress'}: "
    head += f"found {view.verdict_text}" if view.found else "no counterexample"
    if view.wall_seconds is not None:
        head += f"  (wall {view.wall_seconds:.2f}s)"
    out = [head, f"  {view.iterations_run} iteration(s) run"]
    if view.failure_reason:
        out.append(f"  failed: {view.failure_reason}")
    if view.found:
        out.append(f"  seed argv: {' '.join(view.seed_argv)}")
        if view.input_digest:
            out.append(f"  input {view.input_digest}")
    return "\n".join(out)


def render_verification_report(view: VerificationReportView) -> str:
    head = f"{view.problem_name or 'verify'}: {view.match_text}"
    if view.wall_seconds is not None:
        head += f"  (wall {view.wall_seconds:.2f}s)"
    header = ["test"] + [
        f"{column.name} [{column.verdict_text}, {column.match_text}]"
        for column in view.columns
    ]
    rows = [
        [name]
        + [
            "—" if cell is None else f"{cell.verdict.value} {cell.usage.time_text}"
            for cell in cells
        ]
        for name, cells in view.rows()
    ]
    if view.columns:
        rows.append(["cpu/wall"] + [column.time_text for column in view.columns])
    out = [head]
    if view.failure_reason:
        out.append(f"  failed: {view.failure_reason}")
    if header:
        out.append(_table(rows, header=header) if rows or view.columns else "")
    for column in view.columns:
        if column.reason:
            out.append(f"  {column.tag}: {column.reason}")
    for column in view.columns:
        if not column.groups:
            continue
        out.append(f"  {column.tag}: {column.score_text}")
        for group in column.groups:
            mark = "OK" if group.passed else "--"
            line = f"    [{mark}] group {group.group_id} {group.name}: {group.score_text}"
            if group.tag_reason:
                line += f"  ({group.tag_reason})"
            out.append(line)
    return "\n".join(line for line in out if line)
