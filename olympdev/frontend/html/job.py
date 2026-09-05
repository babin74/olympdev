from pathlib import Path

from jinja2 import Environment, FileSystemLoader
from pydantic import Field

from olympdev.schema import FrozenModel
from olympdev.worker.job import JobStatus

_TEMPLATES = Path(__file__).resolve().parent / "templates"
env = Environment(
    loader=FileSystemLoader(_TEMPLATES),
    autoescape=True,
)


class JobRenderContext(FrozenModel):
    job_id: str
    job_name: str
    job_description: str
    job_status: JobStatus
    job_result: str
    job_digests: list[tuple[str, str]]  # (name, digest)
    job_labels: list[tuple[str, str]] = Field(default_factory=list)


def render_job(context: JobRenderContext) -> str:
    template = env.get_template("job.html")
    return template.render(context=context)
