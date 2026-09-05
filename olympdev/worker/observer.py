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


NULL_OBSERVER = NullObserver()
