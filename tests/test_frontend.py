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
    JobView,
    PipelineRunView,
    SolutionColumnView,
    TestingReportView,
    TestRowView,
    UsageView,
    format_duration,
    format_memory,
)
from olympdev.isolate import IsolateMeta
from olympdev.worker.job import JobStatus
from olympdev.worker.pipeline import PipelineStatus
from olympdev.worker.verdict import Verdict

CONTEST = Path(__file__).resolve().parent.parent / "example-contest"


def _report() -> TestingReportView:
    return TestingReportView.from_columns(
        [
            SolutionColumnView(
                name="sol-ac.cpp",
                tag="main",
                verdict=Verdict.OK,
                wall_seconds=1.25,
                tests=[
                    TestRowView(
                        name="sample-01.txt",
                        verdict=Verdict.OK,
                        usage=UsageView(cpu_ms=4.0, wall_ms=5.0, memory_kb=2048),
                    )
                ],
            ),
            SolutionColumnView(name="sol-wa.cpp", tag="WA", verdict=Verdict.WA),
        ],
        problem_name="aplusb",
    )


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


def test_column_totals_cpu_across_tests_against_the_run_wall_clock():
    column = SolutionColumnView(
        name="sol.cpp",
        wall_seconds=3.0,
        tests=[
            TestRowView(
                name="a", verdict=Verdict.OK, usage=UsageView(cpu_ms=100.0, wall_ms=120.0)
            ),
            TestRowView(
                name="b", verdict=Verdict.OK, usage=UsageView(cpu_ms=250.0, wall_ms=260.0)
            ),
        ],
    )
    assert column.cpu_seconds == pytest.approx(0.35)
    assert column.time_text == "350/3000 ms"


def test_column_without_results_reports_no_cpu():
    column = SolutionColumnView(name="sol.cpp", wall_seconds=1.0)
    assert column.cpu_seconds is None
    assert column.time_text == "—/1000 ms"


def test_usage_prefers_cgroup_memory_and_falls_back_to_rss():
    both = IsolateMeta(status="OK", cg_memory_kb=4096, max_rss_kb=99)
    assert UsageView.from_meta(both).memory_kb == 4096
    rss_only = IsolateMeta(status="OK", max_rss_kb=77)
    assert UsageView.from_meta(rss_only).memory_kb == 77
    neither = IsolateMeta(status="OK")
    assert UsageView.from_meta(neither).memory_kb is None


def test_report_rows_align_ragged_columns():
    report = _report()
    assert report.test_names == ["sample-01.txt"]
    (name, cells) = report.rows()[0]
    assert name == "sample-01.txt"
    assert cells[0] is not None and cells[0].verdict is Verdict.OK
    assert cells[1] is None, "a solution with no results leaves an empty cell"


def test_text_report_contains_verdicts_and_wall_times():
    rendered = text_view.render_testing_report(_report())
    assert "aplusb" in rendered
    assert "sol-ac.cpp [OK]" in rendered
    assert "OK 4.0/5.0 ms" in rendered
    assert "cpu/wall" in rendered
    assert "4.0/1250 ms" in rendered, "column totals CPU over the run's wall clock"


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


def test_html_renders_the_same_view_model():
    from olympdev.frontend import html as html_view

    report = _report()
    markup = html_view.render_testing_report(report)
    assert "aplusb" in markup
    assert "sol-ac.cpp" in markup
    assert "od-v-OK" in markup, "verdict colour comes from the shared stylesheet"


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
    assert "<table" in _report()._repr_html_()
    assert "od-card" in PipelineRunView(
        pipeline_id="p", status=PipelineStatus.COMPLETED
    )._repr_html_()


def test_view_layer_does_not_need_jinja(monkeypatch):
    """A worker installs olympdev without the notebook extra; text must still work."""
    import importlib

    monkeypatch.setitem(sys.modules, "jinja2", None)
    for name in ("olympdev.frontend.view", "olympdev.frontend.text"):
        module = importlib.reload(importlib.import_module(name))
        assert module is not None
    assert text_view.render_testing_report(_report())


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
