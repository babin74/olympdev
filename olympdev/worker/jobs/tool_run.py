from pathlib import Path
from typing import ClassVar

from olympdev.isolate import (
    IsolateLimits,
    IsolateMeta,
    TOOL_BINARY,
    TOOL_PROFILE,
    TOOL_RESERVED_PATHS,
    TOOL_STDIN,
    SandboxProfile,
)
from olympdev.schema import FrozenModel
from olympdev.storage import AbstractStorage
from olympdev.worker.cache.policy import CachePolicy
from olympdev.worker.job import JobContext
from olympdev.worker.roles import (
    ROLE_SPECS,
    ProgramRole,
    RoleSpec,
    RunKind,
    job_class_name,
)
from olympdev.worker.jobs.sandbox import (
    SandboxJobResult,
    SandboxRunJob,
    build_job_limits,
    download_job_arguments,
    job_limits_dict,
    parse_job_limits,
    parse_sandbox_files,
    resolve_blob_digest,
    resolve_program_digest,
    resolve_sandbox_files,
)
from olympdev.worker.verdict import Verdict

ToolRunJobResult = SandboxJobResult

# testlib-style checker exits: 0 OK, 1 WA, 2 PE, 3 Fail, 7 points
_CHECKER_WA_EXITS = frozenset({1, 2})


def checker_verdict(meta: IsolateMeta) -> Verdict:
    if meta.is_sandbox_error():
        return Verdict.FL
    if meta.exitcode == 0 and meta.status == "OK" and not meta.cg_oom_killed:
        return Verdict.OK
    if meta.exitcode in _CHECKER_WA_EXITS:
        return Verdict.WA
    if meta.exitcode == 7:
        return Verdict.PT
    return Verdict.CF


class ToolRunJobArguments(FrozenModel):
    program_digest: str
    file_digests: list[str]
    file_paths: list[Path]
    command: list[str]
    stdin_digest: str | None
    limits: IsolateLimits


def _validate_command(command: list[str]) -> list[str]:
    argv = list(command)
    for item in argv:
        if not isinstance(item, str) or "\x00" in item:
            raise ValueError(f"tool command entries must be strings: {item!r}")
    return argv


class ToolRunJob(SandboxRunJob):
    job_type = "tool_run"
    sandbox_profile: ClassVar[SandboxProfile] = TOOL_PROFILE
    role: ClassVar[ProgramRole | None] = None

    def get_description(self) -> str:
        return "Run a tool (checker, validator, or generator)"

    def get_cache_policy(self) -> CachePolicy:
        return CachePolicy.Never

    @classmethod
    def make_input_digest(
        cls,
        storage: AbstractStorage,
        *,
        program: bytes | Path | None = None,
        program_digest: str | None = None,
        files: list[tuple[Path | bytes, str | Path]] | None = None,
        file_digests: list[tuple[str, str | Path]] | None = None,
        command: list[str] | None = None,
        stdin: bytes | str | None = None,
        stdin_digest: str | None = None,
        time_limit_ms: int = 10000,
        wall_time_ms: int = 10000,
        memory_limit_mb: int = 256,
        cg_memory_limit_kb: int | None = None,
        aspace_limit_kb: int | None = None,
        file_size_limit_kb: int = 1024 * 16,
    ) -> str:
        resolved_program_digest = resolve_program_digest(storage, program, program_digest)
        resolved_file_digests, resolved_file_paths = resolve_sandbox_files(
            storage,
            files=files,
            file_digests=file_digests,
            reserved=TOOL_RESERVED_PATHS,
        )
        resolved_stdin_digest = resolve_blob_digest(
            storage,
            stdin,
            stdin_digest,
            field="stdin",
            digest_field="stdin_digest",
        )
        argv = _validate_command(list(command or []))
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
            "files_digests": resolved_file_digests,
            "files_paths": resolved_file_paths,
            "command": argv,
            "stdin_digest": resolved_stdin_digest,
            **job_limits_dict(limits),
        })

    def _parse_arguments(self, context: JobContext) -> ToolRunJobArguments:
        arguments = download_job_arguments(
            context,
            missing="missing tool run job input",
            job_type=type(self).job_type,
        )
        context.storage.validate_digest(arguments["program_digest"])
        file_digests, file_paths = parse_sandbox_files(
            context.storage,
            arguments,
            reserved=TOOL_RESERVED_PATHS,
        )
        stdin_digest = arguments["stdin_digest"]
        if stdin_digest is not None:
            context.storage.validate_digest(stdin_digest)
        command = list(arguments["command"])
        for item in command:
            if not isinstance(item, str):
                raise ValueError("tool command entries must be strings")
        return ToolRunJobArguments(
            program_digest=arguments["program_digest"],
            file_digests=file_digests,
            file_paths=file_paths,
            command=command,
            stdin_digest=stdin_digest,
            limits=parse_job_limits(arguments),
        )

    def _execute(self, context: JobContext) -> str | None:
        arguments = self._parse_arguments(context)
        files: list[tuple[str, str | Path]] = list(
            zip(arguments.file_digests, arguments.file_paths)
        )
        stdin_file = None
        if arguments.stdin_digest is not None:
            files.append((arguments.stdin_digest, TOOL_STDIN))
            stdin_file = TOOL_STDIN
        return self._run_program(
            context,
            program_digest=arguments.program_digest,
            binary_name=TOOL_BINARY,
            files=files,
            stdin_file=stdin_file,
            extra_args=arguments.command,
            limits=arguments.limits,
        )


def _make_tool_run_job_class(spec: RoleSpec) -> type[ToolRunJob]:
    def get_description(self: ToolRunJob) -> str:
        return f"Run {spec.label.lower()}"

    def get_cache_policy(self: ToolRunJob) -> CachePolicy:
        return spec.run_cache_policy

    return type(
        job_class_name(spec.run_job_type),
        (ToolRunJob,),
        {
            "job_type": spec.run_job_type,
            "role": spec.role,
            "get_description": get_description,
            "get_cache_policy": get_cache_policy,
        },
    )


TOOL_RUN_JOB_CLASSES: dict[ProgramRole, type[ToolRunJob]] = {
    role: _make_tool_run_job_class(spec)
    for role, spec in ROLE_SPECS.items()
    if spec.run_kind is RunKind.TOOL
}

GeneratorJob = TOOL_RUN_JOB_CLASSES[ProgramRole.GENERATOR]
ValidatorJob = TOOL_RUN_JOB_CLASSES[ProgramRole.VALIDATOR]
CheckerJob = TOOL_RUN_JOB_CLASSES[ProgramRole.CHECKER]
