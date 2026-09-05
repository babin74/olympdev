"""Drivers that execute the jobs a pipeline issues.

The sequential ``run_pipeline`` lives in ``olympdev.worker.pipeline``. This
module adds a parallel one built on the ``WAITING`` protocol the pipelines
already speak: pull jobs until the pipeline says it is waiting on work in
flight, run those on a pool, and poll again as each finishes.

Concurrency is bounded by the sandbox pool, not by the thread count: each
running job holds one isolate box, so ``max_workers`` defaults to
``isolate.sandbox_count``.
"""

from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait

from olympdev.settings import get_settings
from olympdev.worker.executor import JobExecutor
from olympdev.worker.observer import NULL_OBSERVER, PipelineObserver
from olympdev.worker.pipeline import (
    AbstractPipeline,
    NextJobStatus,
    PipelineContext,
    ScheduledJob,
)


def run_pipeline_parallel(
    pipeline: AbstractPipeline,
    context: PipelineContext,
    *,
    executor: JobExecutor | None = None,
    observer: PipelineObserver | None = None,
    max_workers: int | None = None,
) -> None:
    """Run a pipeline, executing independent jobs concurrently."""
    if executor is None:
        executor = JobExecutor()
    if observer is None:
        observer = NULL_OBSERVER
    if max_workers is None:
        max_workers = get_settings().isolate.sandbox_count
    if max_workers < 1:
        raise ValueError("max_workers must be positive")

    def execute(scheduled: ScheduledJob) -> ScheduledJob:
        executor.execute(scheduled.job, context.job_context(scheduled.input_digest))
        return scheduled

    pending: set[Future[ScheduledJob]] = set()
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        try:
            while True:
                nxt = pipeline.get_next_job(context)
                if nxt is NextJobStatus.FINISHED:
                    break
                if nxt is NextJobStatus.WAITING:
                    if not pending:
                        raise RuntimeError(
                            f"pipeline {pipeline.pipeline_id!r} is waiting "
                            "with no in-flight jobs"
                        )
                    # Poll again as soon as anything finishes: a completed job
                    # usually unblocks the next step.
                    done, pending = wait(pending, return_when=FIRST_COMPLETED)
                    for future in done:
                        observer.on_job_finished(future.result().job)
                    continue
                if len(pending) >= max_workers:
                    done, pending = wait(pending, return_when=FIRST_COMPLETED)
                    for future in done:
                        observer.on_job_finished(future.result().job)
                observer.on_job_issued(nxt.job, nxt.input_digest)
                pending.add(pool.submit(execute, nxt))
        finally:
            # Never leave sandboxes running behind a failure.
            for future in wait(pending).done:
                if future.exception() is None:
                    observer.on_job_finished(future.result().job)
    observer.on_pipeline_finished(pipeline)
