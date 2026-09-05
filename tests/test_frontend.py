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
                        usage=UsageView(elapsed_ms=4.0, memory_kb=2048),
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


def test_timeout_reports_wall_clock_not_cpu():
    """A sleeping solution burns no CPU; showing CPU time would read as 2 ms."""
    meta = IsolateMeta(status="TO", time=0.002, time_wall=2.1, max_rss_kb=512)
    usage = UsageView.from_meta(meta)
    assert usage.elapsed_ms == pytest.approx(2100.0)
    assert usage.elapsed_text == "2100 ms"


def test_normal_run_reports_cpu_time():
    meta = IsolateMeta(status="OK", time=0.25, time_wall=0.9, cg_memory_kb=4096)
    assert UsageView.from_meta(meta).elapsed_ms == pytest.approx(250.0)


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
    assert "OK 4.0 ms" in rendered
    assert "1250 ms" in rendered


def test_text_pipeline_run_lists_tests():
    view = PipelineRunView(
        pipeline_id="p",
        status=PipelineStatus.COMPLETED,
        verdict=Verdict.WA,
        problem_name="aplusb",
        wall_seconds=0.5,
        tests=[TestRowView(name="t", verdict=Verdict.WA)],
    )
    rendered = text_view.render_pipeline_run(view)
    assert "aplusb: WA" in rendered
    assert "500 ms" in rendered
    assert "t" in rendered


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
