from __future__ import annotations

import os
import stat
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, ClassVar, Literal, overload

from olympdev.isolate import (
    RUN_PROFILE,
    IsolateLimits,
    IsolateMeta,
    STDERR_FILE,
    STDOUT_FILE,
    SandboxProfile,
    sandbox_relpath,
)
from olympdev.schema import StorageModel
from olympdev.storage import AbstractStorage
from olympdev.worker.job import AbstractJob, JobContext, JobStatus, JOB_TYPE_KEY

_DEFAULT_FILE_SIZE_LIMIT_KB = 16 * 1024


class SandboxJobResult(StorageModel):
    stdout_digest: str
    stderr_digest: str
    meta: IsolateMeta


def resolve_program_digest(
    storage: AbstractStorage,
    program: bytes | Path | None,
    program_digest: str | None,
) -> str:
    if (program is None) == (program_digest is None):
        raise ValueError("exactly one of program or program_digest must be provided")
    if isinstance(program, Path):
        digest = storage.upload_file(program)
    elif isinstance(program, bytes):
        digest = storage.upload_bytes(program)
    else:
        digest = program_digest
    assert digest is not None
    storage.validate_digest(digest)
    return digest


@overload
def resolve_blob_digest(
    storage: AbstractStorage,
    data: bytes | str | None,
    digest: str | None,
    *,
    field: str,
    digest_field: str,
    missing: Literal["empty"],
) -> str: ...


@overload
def resolve_blob_digest(
    storage: AbstractStorage,
    data: bytes | str | None,
    digest: str | None,
    *,
    field: str,
    digest_field: str,
    missing: Literal["none"] = "none",
) -> str | None: ...


def resolve_blob_digest(
    storage: AbstractStorage,
    data: bytes | str | None,
    digest: str | None,
    *,
    field: str,
    digest_field: str,
    missing: Literal["empty", "none"] = "none",
) -> str | None:
    if data is not None and digest is not None:
        raise ValueError(f"{field} and {digest_field} are mutually exclusive")
    if digest is not None:
        return storage.validate_digest(digest)
    if data is None:
        if missing == "none":
            return None
        data = b""
    if isinstance(data, str):
        data = data.encode()
    return storage.upload_bytes(data)


def build_job_limits(
    *,
    time_limit_ms: int,
    wall_time_ms: int,
    memory_limit_mb: int,
    cg_memory_limit_kb: int | None = None,
    aspace_limit_kb: int | None = None,
    file_size_limit_kb: int = _DEFAULT_FILE_SIZE_LIMIT_KB,
) -> IsolateLimits:
    if cg_memory_limit_kb is None:
        cg_memory_limit_kb = memory_limit_mb * 1024
    return IsolateLimits(
        time_limit_ms=time_limit_ms,
        wall_time_ms=wall_time_ms,
        memory_limit_mb=memory_limit_mb,
        cg_memory_limit_kb=cg_memory_limit_kb,
        aspace_limit_kb=aspace_limit_kb,
        file_size_limit_kb=file_size_limit_kb,
    )


def job_limits_dict(limits: IsolateLimits) -> dict[str, Any]:
    return {
        "time_limit_ms": limits.time_limit_ms,
        "wall_time_ms": limits.wall_time_ms,
        "memory_limit_mb": limits.memory_limit_mb,
        "cg_memory_limit_kb": limits.cg_memory_limit_kb,
        "aspace_limit_kb": limits.aspace_limit_kb,
        "file_size_limit_kb": limits.file_size_limit_kb,
    }


def parse_job_limits(arguments: Mapping[str, Any]) -> IsolateLimits:
    limits = IsolateLimits.from_mapping(arguments)
    return IsolateLimits(
        time_limit_ms=limits.time_limit_ms,
        wall_time_ms=limits.wall_time_ms or limits.time_limit_ms,
        memory_limit_mb=limits.memory_limit_mb,
        cg_memory_limit_kb=limits.cg_memory_limit_kb or limits.memory_kb(),
        aspace_limit_kb=limits.aspace_limit_kb,
        file_size_limit_kb=limits.file_size_limit_kb or _DEFAULT_FILE_SIZE_LIMIT_KB,
    )


def resolve_sandbox_files(
    storage: AbstractStorage,
    *,
    files: Sequence[tuple[Path | bytes, str | Path]] | None = None,
    file_digests: Sequence[tuple[str, str | Path]] | None = None,
    reserved: frozenset[str],
) -> tuple[list[str], list[str]]:
    resolved_digests: list[str] = []
    resolved_paths: list[str] = []
    seen: set[str] = set()
    for source, sandbox_path in files or []:
        rel = sandbox_relpath(sandbox_path, reserved=reserved)
        if rel in seen:
            raise ValueError(f"duplicate sandbox path: {rel}")
        seen.add(rel)
        if isinstance(source, bytes):
            resolved_digests.append(storage.upload_bytes(source))
        else:
            resolved_digests.append(storage.upload_file(source))
        resolved_paths.append(rel)
    for digest, sandbox_path in file_digests or []:
        rel = sandbox_relpath(sandbox_path, reserved=reserved)
        if rel in seen:
            raise ValueError(f"duplicate sandbox path: {rel}")
        seen.add(rel)
        storage.validate_digest(digest)
        resolved_digests.append(digest)
        resolved_paths.append(rel)
    return resolved_digests, resolved_paths


def parse_sandbox_files(
    storage: AbstractStorage,
    arguments: Mapping[str, Any],
    *,
    reserved: frozenset[str],
) -> tuple[list[str], list[Path]]:
    file_paths = [
        Path(sandbox_relpath(path, reserved=reserved))
        for path in arguments["files_paths"]
    ]
    file_digests = list(arguments["files_digests"])
    if len(file_digests) != len(file_paths):
        raise ValueError("files_digests and files_paths length mismatch")
    for digest in file_digests:
        storage.validate_digest(digest)
    return file_digests, file_paths


def download_job_arguments(
    context: JobContext, *, missing: str, job_type: str
) -> dict[str, Any]:
    arguments = context.storage.download_dictionary(context.input_digest)
    if arguments is None:
        raise ValueError(missing)
    actual = arguments.get(JOB_TYPE_KEY)
    if actual != job_type:
        raise ValueError(
            f"job_type mismatch: expected {job_type!r}, got {actual!r}"
        )
    return arguments


def chmod_executable(path: Path) -> None:
    os.chmod(
        path,
        path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH,
    )


class SandboxRunJob(AbstractJob):
    #: Sandbox profile this job's program runs under.
    sandbox_profile: ClassVar[SandboxProfile] = RUN_PROFILE

    def get_result(self, storage: AbstractStorage) -> SandboxJobResult | None:
        digest = self.get_output_digest()
        if digest is None:
            return None
        return SandboxJobResult.from_storage(storage, digest)

    def _run_program(
        self,
        context: JobContext,
        *,
        program_digest: str,
        binary_name: str,
        files: Sequence[tuple[str, str | Path]] = (),
        stdin_file: str | None = None,
        extra_args: Sequence[str] = (),
        limits: IsolateLimits,
    ) -> str | None:
        storage = context.storage
        if context.isolate_factory is None:
            raise RuntimeError(f"isolate_factory is required for {type(self).__name__}")

        with context.isolate_factory.get_sandbox() as sandbox:
            binary_path = sandbox.box_path(binary_name)
            storage.download_file(program_digest, binary_path)
            chmod_executable(binary_path)
            for file_digest, file_path in files:
                storage.download_file(file_digest, sandbox.box_path(file_path))

            meta = sandbox.run(
                [f"./{binary_name}", *extra_args],
                limits,
                type(self).sandbox_profile,
                stdin_file=stdin_file,
                stdout_file=STDOUT_FILE,
                stderr_file=STDERR_FILE,
            )
            result = SandboxJobResult(
                stdout_digest=storage.upload_file(sandbox.box_path(STDOUT_FILE)),
                stderr_digest=storage.upload_file(sandbox.box_path(STDERR_FILE)),
                meta=meta,
            )
            self.job_result = result.upload(storage)
            if meta.is_sandbox_error():
                self.job_status = JobStatus.FAILED
                self.job_failure_reason = "Sandbox run failed"
                return None
            return self.job_result
