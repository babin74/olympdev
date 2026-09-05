from pathlib import Path

from olympdev.isolate import IsolateLimits, SIMPLE_RUN_BINARY, SIMPLE_RUN_INPUT
from olympdev.schema import FrozenModel
from olympdev.storage import AbstractStorage
from olympdev.worker.cache.policy import CachePolicy
from olympdev.worker.job import JobContext
from olympdev.worker.jobs.sandbox import (
    SandboxJobResult,
    SandboxRunJob,
    build_job_limits,
    download_job_arguments,
    job_limits_dict,
    parse_job_limits,
    resolve_blob_digest,
    resolve_program_digest,
)

SimpleRunJobResult = SandboxJobResult


class SimpleRunJobArguments(FrozenModel):
    program_digest: str
    input_digest: str
    limits: IsolateLimits


class SimpleRunJob(SandboxRunJob):
    job_type = "simple_run"

    def get_description(self) -> str:
        return "Run a program on given input"

    def get_cache_policy(self) -> CachePolicy:
        return CachePolicy.Never

    @classmethod
    def make_input_digest(
        cls,
        storage: AbstractStorage,
        *,
        program: bytes | Path | None = None,
        program_digest: str | None = None,
        input: bytes | str | None = None,
        input_digest: str | None = None,
        time_limit_ms: int = 1000,
        wall_time_ms: int = 1000,
        memory_limit_mb: int = 64,
        cg_memory_limit_kb: int | None = None,
        aspace_limit_kb: int | None = None,
        file_size_limit_kb: int = 1024 * 16,
    ) -> str:
        resolved_program_digest = resolve_program_digest(storage, program, program_digest)
        resolved_input_digest = resolve_blob_digest(
            storage,
            input,
            input_digest,
            field="input",
            digest_field="input_digest",
            missing="empty",
        )
        limits = build_job_limits(
            time_limit_ms=time_limit_ms,
            wall_time_ms=wall_time_ms,
            memory_limit_mb=memory_limit_mb,
            cg_memory_limit_kb=cg_memory_limit_kb,
            aspace_limit_kb=aspace_limit_kb,
            file_size_limit_kb=file_size_limit_kb,
        )
        return cls._upload_input(storage, {
            "program_digest": resolved_program_digest,
            "input_digest": resolved_input_digest,
            **job_limits_dict(limits),
        })

    def _parse_arguments(self, context: JobContext) -> SimpleRunJobArguments:
        arguments = download_job_arguments(
            context,
            missing="missing simple run job input",
            job_type=type(self).job_type,
        )
        context.storage.validate_digest(arguments["program_digest"])
        context.storage.validate_digest(arguments["input_digest"])
        return SimpleRunJobArguments(
            program_digest=arguments["program_digest"],
            input_digest=arguments["input_digest"],
            limits=parse_job_limits(arguments),
        )

    def _execute(self, context: JobContext) -> str | None:
        arguments = self._parse_arguments(context)
        return self._run_program(
            context,
            program_digest=arguments.program_digest,
            binary_name=SIMPLE_RUN_BINARY,
            files=[(arguments.input_digest, SIMPLE_RUN_INPUT)],
            stdin_file=SIMPLE_RUN_INPUT,
            limits=arguments.limits,
        )


class SolutionRunJob(SimpleRunJob):
    job_type = "solution_run"

    def get_description(self) -> str:
        return "Run a participant solution"

    def get_cache_policy(self) -> CachePolicy:
        return CachePolicy.SolutionRun


class ModelSolutionRunJob(SimpleRunJob):
    job_type = "model_solution_run"

    def get_description(self) -> str:
        return "Run a model solution"

    def get_cache_policy(self) -> CachePolicy:
        return CachePolicy.ModelSolutionRun
