"""HTML rendering of the view models. Requires the `notebook` extra (jinja2)."""

from olympdev.frontend.html.job import (
    JobRenderContext,
    render_job,
    render_pipeline_run,
    render_statement_result,
    render_stress_result,
    render_verification_report,
)

__all__ = [
    "JobRenderContext",
    "render_job",
    "render_pipeline_run",
    "render_statement_result",
    "render_stress_result",
    "render_verification_report",
]
