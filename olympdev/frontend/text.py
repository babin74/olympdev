"""Terminal rendering of the same view models the HTML templates use."""

from __future__ import annotations

from olympdev.frontend.view import PipelineRunView, TestingReportView


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


def render_testing_report(view: TestingReportView) -> str:
    header = ["test"] + [
        f"{column.name} [{column.verdict_text}]" for column in view.columns
    ]
    rows = [
        [name]
        + [
            "—" if cell is None else f"{cell.verdict.value} {cell.usage.time_text}"
            for cell in cells
        ]
        for name, cells in view.rows()
    ]
    rows.append(["cpu/wall"] + [column.time_text for column in view.columns])
    title = f"{view.problem_name}\n" if view.problem_name else ""
    return title + _table(rows, header=header)
