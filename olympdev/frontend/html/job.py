"""Render view models as standalone HTML fragments.

Templates take a view model and nothing else, and pull their styling from
_base.css, so there is exactly one place where a verdict colour or a card
border is defined.
"""

from __future__ import annotations

from functools import cache
from pathlib import Path

from olympdev.frontend.view import (
    JobView,
    PipelineRunView,
    StatementResultView,
    StressResultView,
    VerificationReportView,
)

_TEMPLATES = Path(__file__).resolve().parent / "templates"

#: Kept for callers that imported the old name.
JobRenderContext = JobView


@cache
def _environment():
    try:
        from jinja2 import Environment, FileSystemLoader
    except ModuleNotFoundError as exc:  # pragma: no cover - depends on install
        raise ModuleNotFoundError(
            "HTML rendering needs jinja2: pip install 'olympdev[notebook]'"
        ) from exc
    return Environment(loader=FileSystemLoader(_TEMPLATES), autoescape=True)


def _render(template_name: str, view: object) -> str:
    return _environment().get_template(template_name).render(view=view)


def render_job(view: JobView) -> str:
    return _render("job.html", view)


def render_pipeline_run(view: PipelineRunView) -> str:
    return _render("pipeline_run.html", view)


def render_verification_report(view: VerificationReportView) -> str:
    return _render("verification_report.html", view)


def render_statement_result(view: StatementResultView) -> str:
    return _render("statement_result.html", view)


def render_stress_result(view: StressResultView) -> str:
    return _render("stress_result.html", view)
