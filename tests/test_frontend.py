"""View models, HTML rendering and the CLI.

These cover what used to be ad-hoc glue in a notebook: usage formatting, the
timeout/wall-clock rule, escaping, and the fact that text and HTML render the
same view model.
"""

import json
import sys
from pathlib import Path

import pytest

from olympdev.frontend import text as text_view
from olympdev.frontend.cli.main import main
from olympdev.frontend.view import (
    GroupScoreView,
    JobView,
    PipelineRunView,
    SolutionColumnView,
    TestRowView,
    UsageView,
    VerificationReportView,
    format_duration,
    format_memory,
)
from olympdev.isolate import IsolateMeta
from olympdev.worker.job import JobStatus
from olympdev.worker.pipeline import PipelineStatus
from olympdev.worker.verdict import Verdict

CONTEST = Path(__file__).resolve().parent.parent / "example-contest"


def _verification_report() -> VerificationReportView:
    column = SolutionColumnView(
        name="sol-ac",
        tag="main",
        verdict=Verdict.OK,
        matched=True,
        tests=[
            TestRowView(
                name="sample-01.txt",
                verdict=Verdict.OK,
                usage=UsageView(cpu_ms=4.0, wall_ms=5.0, memory_kb=2048),
            )
        ],
    )
    mismatch = SolutionColumnView(
        name="sol-wa",
        tag="WA",
        verdict=Verdict.OK,
        matched=False,
        reason="missing required verdict WA",
    )
    return VerificationReportView(
        matched=False,
        status=PipelineStatus.COMPLETED,
        problem_name="aplusb",
        test_names=["sample-01.txt"],
        columns=[column, mismatch],
    )


def _verification_report_with_groups() -> VerificationReportView:
    column = SolutionColumnView(
        name="sol-overflow",
        tag="WA",
        verdict=Verdict.WA,
        matched=False,
        reason="group 2: unexpected verdict WA on tests/g2-01.txt",
        total_points=40,
        max_points=100,
        groups=[
            GroupScoreView(
                group_id=1, name="small", max_points=40, points_earned=40, passed=True
            ),
            GroupScoreView(
                group_id=2,
                name="large",
                max_points=60,
                points_earned=0,
                passed=False,
                failing_tests=["tests/g2-01.txt"],
                tag_checked=True,
                tag_reason="unexpected verdict WA on tests/g2-01.txt",
            ),
        ],
    )
    return VerificationReportView(
        matched=False,
        status=PipelineStatus.COMPLETED,
        problem_name="points",
        test_names=[],
        columns=[column],
    )


def test_two_solutions_sharing_a_tag_are_reported_apart():
    """Tags repeat now, so a reason line must be labelled by name, not tag."""
    view = VerificationReportView(
        matched=False,
        status=PipelineStatus.COMPLETED,
        problem_name="aplusb",
        test_names=[],
        columns=[
            SolutionColumnView(
                name="sol-overflow",
                tag="WA",
                verdict=Verdict.OK,
                matched=False,
                reason="missing required verdict WA",
            ),
            SolutionColumnView(
                name="sol-off-by-one",
                tag="WA",
                verdict=Verdict.RE,
                matched=False,
                reason="unexpected verdict RE on tests/03.txt",
            ),
        ],
    )
    rendered = text_view.render_verification_report(view)
    assert "sol-overflow: missing required verdict WA" in rendered
    assert "sol-off-by-one: unexpected verdict RE on tests/03.txt" in rendered


def test_text_verification_report_shows_group_scores():
    rendered = text_view.render_verification_report(_verification_report_with_groups())
    assert "sol-overflow: 40/100" in rendered
    assert "[OK] group 1 small: 40/40" in rendered
    assert "[--] group 2 large: 0/60" in rendered
    assert "unexpected verdict WA" in rendered


def test_html_verification_report_includes_group_scores():
    from olympdev.frontend import html as html_view

    markup = html_view.render_verification_report(_verification_report_with_groups())
    assert '<div class="od-subgrid">' in markup
    assert "40/40" in markup
    assert "0/60" in markup
    assert "small" in markup and "large" in markup


def test_ungrouped_report_renders_no_group_markup():
    """No groups on any column -> no group section at all (byte-identical invariant)."""
    from olympdev.frontend import html as html_view

    rendered = text_view.render_verification_report(_verification_report())
    assert "group" not in rendered
    assert "stderr" not in rendered, "a report with nothing to diagnose stays quiet"
    markup = html_view.render_verification_report(_verification_report())
    assert '<div class="od-subgrid">' not in markup


DIGEST = "a" * 64
OTHER_DIGEST = "b" * 64


def _run_view(verdict: Verdict, **row) -> PipelineRunView:
    return PipelineRunView(
        pipeline_id="p",
        status=PipelineStatus.COMPLETED,
        verdict=verdict,
        problem_name="aplusb",
        tests=[TestRowView(name="tests/03.txt", verdict=verdict, **row)],
    )


def test_a_failing_run_names_the_stderr_to_read():
    rendered = text_view.render_pipeline_run(
        _run_view(Verdict.RE, stderr_digest=DIGEST)
    )
    assert f"tests/03.txt: RE  stderr {DIGEST}" in rendered


def test_a_passing_run_offers_no_digests():
    rendered = text_view.render_pipeline_run(
        _run_view(Verdict.OK, stdout_digest=DIGEST, stderr_digest=OTHER_DIGEST)
    )
    assert "stderr" not in rendered and "stdout" not in rendered


def test_digests_flag_lists_every_test():
    rendered = text_view.render_pipeline_run(
        _run_view(Verdict.OK, input_digest=DIGEST, stdout_digest=OTHER_DIGEST),
        digests=True,
    )
    assert f"input {DIGEST}" in rendered
    assert f"stdout {OTHER_DIGEST}" in rendered


def test_a_matching_column_diagnoses_nothing():
    """A WA test on a WA-tagged solution is the point of it, not a symptom."""
    column = SolutionColumnView(
        name="sol-wa",
        tag="WA",
        verdict=Verdict.WA,
        matched=True,
        tests=[
            TestRowView(name="tests/01.txt", verdict=Verdict.WA, stderr_digest=DIGEST)
        ],
    )
    assert column.diagnostics == []


def test_a_mismatching_column_points_at_the_failing_test():
    column = SolutionColumnView(
        name="sol-ml",
        tag="ML",
        verdict=Verdict.RE,
        matched=False,
        reason="unexpected verdict RE on tests/01.txt",
        tests=[
            TestRowView(name="tests/01.txt", verdict=Verdict.RE, stderr_digest=DIGEST)
        ],
    )
    view = VerificationReportView(
        matched=False,
        status=PipelineStatus.COMPLETED,
        problem_name="aplusb",
        test_names=["tests/01.txt"],
        columns=[column],
    )
    rendered = text_view.render_verification_report(view)
    assert "sol-ml: unexpected verdict RE on tests/01.txt" in rendered
    assert f"tests/01.txt  stderr {DIGEST}" in rendered


def test_an_empty_stream_is_not_offered_as_a_digest(tmp_path: Path):
    """A digest of zero bytes is nothing to look at, so it is not shown."""
    from olympdev.storage import FileStorage
    from olympdev.worker.jobs.sandbox import SandboxJobResult
    from olympdev.worker.pipelines.problem_test import ProblemTestResult

    storage = FileStorage(tmp_path / "store")
    empty = storage.upload_bytes(b"")
    spoken = storage.upload_bytes(b"boom\n")
    run_digest = SandboxJobResult(
        stdout_digest=spoken,
        stderr_digest=empty,
        meta=IsolateMeta(status="OK", time=0.1, time_wall=0.1),
    ).upload(storage)
    row = TestRowView.from_result(
        storage,
        ProblemTestResult(
            name="tests/01.txt",
            verdict=Verdict.OK,
            run_digest=run_digest,
            input_digest=empty,
        ),
    )
    assert row.stdout_digest == spoken
    assert row.stderr_digest is None
    assert row.input_digest is None


@pytest.mark.parametrize(
    "milliseconds,expected",
    [(None, "—"), (0.5, "0.5 ms"), (9.99, "10.0 ms"), (1234.0, "1234 ms")],
)
def test_duration_formatting(milliseconds, expected):
    assert format_duration(milliseconds) == expected


@pytest.mark.parametrize(
    "kilobytes,expected", [(None, "—"), (512, "512 KB"), (2048, "2.0 MB")]
)
def test_memory_formatting(kilobytes, expected):
    assert format_memory(kilobytes) == expected


def test_usage_keeps_both_clocks():
    meta = IsolateMeta(status="OK", time=0.25, time_wall=0.31, cg_memory_kb=4096)
    usage = UsageView.from_meta(meta)
    assert usage.cpu_ms == pytest.approx(250.0)
    assert usage.wall_ms == pytest.approx(310.0)
    assert usage.time_text == "250/310 ms"


def test_a_sleeping_timeout_is_distinguishable_from_a_busy_one():
    """Either clock alone hides half the story, so both are shown."""
    sleeping = UsageView.from_meta(
        IsolateMeta(status="TO", time=0.002, time_wall=2.1, max_rss_kb=512)
    )
    busy = UsageView.from_meta(
        IsolateMeta(status="TO", time=2.05, time_wall=2.1, max_rss_kb=512)
    )
    assert sleeping.time_text == "2.0/2100 ms"
    assert busy.time_text == "2050/2100 ms"
    assert sleeping.time_text != busy.time_text


def test_time_text_is_a_dash_when_nothing_was_measured():
    assert UsageView().time_text == "—"


def test_column_time_text_is_the_peak_test_not_the_total():
    """Peaks each clock independently - the worst CPU test need not be the worst wall test."""
    column = SolutionColumnView(
        name="sol.cpp",
        tests=[
            TestRowView(
                name="a", verdict=Verdict.OK, usage=UsageView(cpu_ms=250.0, wall_ms=120.0)
            ),
            TestRowView(
                name="b", verdict=Verdict.OK, usage=UsageView(cpu_ms=100.0, wall_ms=260.0)
            ),
        ],
    )
    assert column.peak_cpu_ms == 250.0
    assert column.peak_wall_ms == 260.0
    assert column.time_text == "250/260 ms"


def test_column_without_results_reports_no_peak():
    column = SolutionColumnView(name="sol.cpp")
    assert column.peak_cpu_ms is None
    assert column.peak_wall_ms is None
    assert column.time_text == "—"


def test_usage_prefers_cgroup_memory_and_falls_back_to_rss():
    both = IsolateMeta(status="OK", cg_memory_kb=4096, max_rss_kb=99)
    assert UsageView.from_meta(both).memory_kb == 4096
    rss_only = IsolateMeta(status="OK", max_rss_kb=77)
    assert UsageView.from_meta(rss_only).memory_kb == 77
    neither = IsolateMeta(status="OK")
    assert UsageView.from_meta(neither).memory_kb is None


def test_report_rows_align_ragged_columns():
    report = _verification_report()
    assert report.test_names == ["sample-01.txt"]
    (name, cells) = report.rows()[0]
    assert name == "sample-01.txt"
    assert cells[0] is not None and cells[0].verdict is Verdict.OK
    assert cells[1] is None, "a solution with no results leaves an empty cell"


def test_text_pipeline_run_lists_tests():
    view = PipelineRunView(
        pipeline_id="p",
        status=PipelineStatus.COMPLETED,
        verdict=Verdict.WA,
        problem_name="aplusb",
        wall_seconds=0.5,
        tests=[
            TestRowView(
                name="t", verdict=Verdict.WA, usage=UsageView(cpu_ms=12.0, wall_ms=20.0)
            )
        ],
    )
    rendered = text_view.render_pipeline_run(view)
    assert "aplusb: WA" in rendered
    assert "cpu/wall 12/500 ms" in rendered
    assert "12/20 ms" in rendered


def test_text_verification_report_shows_match_and_reason():
    rendered = text_view.render_verification_report(_verification_report())
    assert "aplusb: mismatch" in rendered
    assert "sol-ac [OK, match]" in rendered
    assert "sol-wa [OK, mismatch]" in rendered
    assert "OK 4.0/5.0 ms" in rendered
    assert "cpu/wall" in rendered
    assert "4.0/5.0 ms" in rendered, "column header peaks the same test's cpu/wall"
    assert "missing required verdict WA" in rendered


def test_html_verification_report_uses_the_view_model():
    from olympdev.frontend import html as html_view

    markup = html_view.render_verification_report(_verification_report())
    assert "aplusb" in markup
    assert "sol-ac" in markup
    assert "od-v-OK" in markup, "verdict colour comes from the shared stylesheet"
    assert "od-v-match" in markup
    assert "od-v-mismatch" in markup
    assert "od-card" in markup
    assert "od-badge" in markup
    assert "missing required verdict WA" in markup


def test_html_escapes_untrusted_text():
    """Labels and names reach the template from problem files; autoescape holds."""
    from olympdev.frontend import html as html_view

    view = JobView(
        job_id="j",
        job_name="J",
        job_status=JobStatus.COMPLETED,
        job_labels=[("Test", "<script>alert(1)</script>")],
    )
    markup = html_view.render_job(view)
    assert "<script>alert(1)</script>" not in markup
    assert "&lt;script&gt;" in markup


def test_views_render_themselves_in_a_notebook():
    assert "<table" in _verification_report()._repr_html_()
    assert "od-card" in _verification_report()._repr_html_()
    assert "od-card" in PipelineRunView(
        pipeline_id="p", status=PipelineStatus.COMPLETED
    )._repr_html_()


#: Imports the view layer with jinja2 made unimportable, then renders a
#: report. Run in a subprocess on purpose: reloading a module in-process
#: re-executes it into the *same* globals dict, so every module that already
#: imported a class from it keeps a stale reference while the dict gets fresh
#: classes - which made pydantic reject its own models in whatever test
#: happened to run next.
_NO_JINJA_PROGRAM = """
import sys


class BlockJinja:
    def find_spec(self, name, path=None, target=None):
        if name == "jinja2" or name.startswith("jinja2."):
            raise ImportError("jinja2 is not installed")
        return None


sys.meta_path.insert(0, BlockJinja())

from olympdev.frontend import text as text_view
from olympdev.frontend.view import SolutionColumnView, VerificationReportView
from olympdev.worker.pipeline import PipelineStatus
from olympdev.worker.verdict import Verdict

view = VerificationReportView(
    matched=False,
    status=PipelineStatus.COMPLETED,
    problem_name="aplusb",
    test_names=[],
    columns=[
        SolutionColumnView(
            name="sol-wa",
            tag="WA",
            verdict=Verdict.OK,
            matched=False,
            reason="missing required verdict WA",
        )
    ],
)
rendered = text_view.render_verification_report(view)
assert "sol-wa" in rendered, rendered
assert "jinja2" not in sys.modules
print("ok")
"""


def test_view_layer_does_not_need_jinja():
    """A worker may never render HTML; the text report must not need jinja2."""
    import subprocess

    result = subprocess.run(
        [sys.executable, "-c", _NO_JINJA_PROGRAM],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "ok" in result.stdout


def test_cli_config_reports_its_source(capsys, tmp_path: Path):
    config = tmp_path / "olympdev.toml"
    config.write_text("[limits]\nmax_memory_limit_mb = 128\n")
    assert main(["--config", str(config), "config"]) == 0
    out = capsys.readouterr().out
    assert str(config) in out
    assert json.loads(out.split("\n", 1)[1])["limits"]["max_memory_limit_mb"] == 128


def test_cli_show_reports_a_missing_blob(capsys, tmp_path: Path):
    config = tmp_path / "olympdev.toml"
    config.write_text(f'[storage]\nroot = "{tmp_path / "store"}"\n')
    assert main(["--config", str(config), "show", "0" * 64]) == 1
    assert "no blob" in capsys.readouterr().err


def test_cli_reports_a_bad_target_without_a_traceback(capsys, tmp_path: Path):
    assert main(["run", str(tmp_path)]) == 2
    assert "error:" in capsys.readouterr().err


def test_cli_compile_commands_still_works(capsys, tmp_path: Path):
    target = CONTEST / "aplusb" / "info.json"
    assert main(["compile-commands", str(target)]) == 0
    written = Path(capsys.readouterr().out.strip())
    assert written.is_file()
    entries = json.loads(written.read_text())
    assert entries and all("arguments" in entry for entry in entries)


def test_cli_compile_commands_takes_a_directory(capsys):
    """Every other command accepts a directory target; this one used to
    hand back an IsADirectoryError traceback instead."""
    assert main(["compile-commands", str(CONTEST / "aplusb")]) == 0
    assert capsys.readouterr().out.strip()
