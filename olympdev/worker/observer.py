"""Watching a pipeline run without forking the driver.

Every driver reports the same three events, so progress output, notebook
rendering and CLI logging are all one small class each instead of a copied
run loop that drifts from the original.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from olympdev.worker.job import AbstractJob
    from olympdev.worker.pipeline import AbstractPipeline


@runtime_checkable
class PipelineObserver(Protocol):
    def on_job_issued(self, job: "AbstractJob", input_digest: str) -> None: ...

    def on_job_finished(self, job: "AbstractJob") -> None: ...

    def on_pipeline_finished(self, pipeline: "AbstractPipeline") -> None: ...


class NullObserver:
    def on_job_issued(self, job: "AbstractJob", input_digest: str) -> None:
        return

    def on_job_finished(self, job: "AbstractJob") -> None:
        return

    def on_pipeline_finished(self, pipeline: "AbstractPipeline") -> None:
        return


class TextObserver:
    """One line per job, for the CLI."""

    def __init__(self, write=print, *, show_digests: bool = False):
        self._write = write
        self._show_digests = show_digests

    def on_job_issued(self, job: "AbstractJob", input_digest: str) -> None:
        suffix = f"  {input_digest[:12]}" if self._show_digests else ""
        self._write(f"  -> {job.job_id}{suffix}")

    def on_job_finished(self, job: "AbstractJob") -> None:
        status = job.get_status().value
        reason = job.get_failure_reason()
        detail = f": {reason}" if reason else ""
        self._write(f"  {status:<9} {job.job_id}{detail}")

    def on_pipeline_finished(self, pipeline: "AbstractPipeline") -> None:
        reason = pipeline.get_failure_reason()
        detail = f": {reason}" if reason else ""
        self._write(f"{pipeline.pipeline_id}: {pipeline.get_status().value}{detail}")


NULL_OBSERVER = NullObserver()
