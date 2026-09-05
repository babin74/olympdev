import re
from pathlib import Path

from olympdev.isolate import (
    COMPILE_PROFILE,
    COMPILE_RESERVED_PATHS,
    IsolateLimits,
    IsolateMeta,
    sandbox_relpath,
)
from olympdev.schema import FrozenModel, StorageModel
from olympdev.settings import Language, get_settings
from olympdev.storage import AbstractStorage
from olympdev.worker.cache.policy import CachePolicy
from olympdev.worker.job import AbstractJob, JobContext, JobStatus

_COMPILER_NAME = re.compile(
    r"^(?:g\+\+|gcc|clang\+\+|clang|c\+\+|cc|python3|python)(?:-\d+(?:\.\d+)*)?$"
)
_COMPILER_DIRS = (
    Path("/usr/bin"),
    Path("/bin"),
    Path("/usr/local/bin"),
    Path("/usr/libexec"),
    Path("/usr/lib/ccache"),
)
_FORBIDDEN_COMPILE_PREFIXES = (
    "-fplugin",
    "-load",
    "-wrapper",
    "-specs",
    "-B",
    "-plugin",
    "-iplugindir",
)


_C_COMPILER_NAMES = frozenset({"gcc", "cc"})


def _compiler_language(compiler_path: Path) -> Language:
    """Language of a compiler, for picking its toolchain settings."""
    stem = compiler_path.name.split("-", 1)[0]
    return Language.C if stem in _C_COMPILER_NAMES else Language.CPP


def _check_flag_allowed(arg: str, allowed_flags: frozenset[str] | None) -> None:
    """Reject a flag by allowlist when configured, else by the denylist."""
    if allowed_flags is not None:
        if arg not in allowed_flags:
            raise ValueError(f"compile flag is not in the allowlist: {arg}")
        return
    if arg.startswith("@") or any(
        arg == prefix or arg.startswith(prefix)
        for prefix in _FORBIDDEN_COMPILE_PREFIXES
    ):
        raise ValueError(f"compile flag is not allowed: {arg}")


def _compiler_dir_allowed(path: Path) -> bool:
    parent = path.resolve().parent
    for allowed in _COMPILER_DIRS:
        try:
            if parent == allowed or parent.is_relative_to(allowed):
                return True
        except ValueError:
            continue
    posix = parent.as_posix()
    return posix.startswith("/usr/lib/llvm") and posix.endswith("/bin")


def validate_compile_command(
    command: list[str | int],
    file_count: int,
    *,
    reserved: frozenset[str] = COMPILE_RESERVED_PATHS,
) -> list[str | int]:
    if not command:
        raise ValueError("compile command must not be empty")
    compiler = command[0]
    if not isinstance(compiler, str):
        raise ValueError("compile command[0] must be an absolute compiler path")
    compiler_path = Path(compiler)
    if not compiler_path.is_absolute():
        raise ValueError(f"compiler path must be absolute: {compiler!r}")
    if not _COMPILER_NAME.fullmatch(compiler_path.name):
        raise ValueError(f"compiler is not allowed: {compiler_path.name!r}")
    if not _compiler_dir_allowed(compiler_path):
        raise ValueError(f"compiler path is not in an allowed directory: {compiler}")
    if not compiler_path.is_file():
        raise ValueError(f"compiler does not exist: {compiler}")

    toolchain = get_settings().toolchain(_compiler_language(compiler_path))
    allowed_flags = (
        None if toolchain.allowed_flags is None else frozenset(toolchain.allowed_flags)
    )

    validated: list[str | int] = [compiler]
    skip_next_as_output = False
    for arg in command[1:]:
        if skip_next_as_output:
            if not isinstance(arg, str):
                raise ValueError("compile -o output must be a sandbox-relative path")
            validated.append(sandbox_relpath(arg, reserved=reserved))
            skip_next_as_output = False
            continue
        if isinstance(arg, bool) or not isinstance(arg, (str, int)):
            raise ValueError(f"invalid compile argument: {arg!r}")
        if isinstance(arg, int):
            if arg < 0 or arg >= file_count:
                raise ValueError(f"compile command index out of range: {arg}")
            validated.append(arg)
            continue
        if arg == "":
            raise ValueError("empty compile argument")
        if arg == "-o":
            skip_next_as_output = True
            validated.append(arg)
            continue
        _check_flag_allowed(arg, allowed_flags)
        validated.append(arg)
    if skip_next_as_output:
        raise ValueError("compile -o is missing an output path")
    return validated


class CompileJobArguments(FrozenModel):
    file_digests: list[str]
    file_paths: list[Path]
    command: list[str | int]

    def to_cli_arguments(self, box_file_paths: list[str]) -> list[str]:
        result = []
        for x in self.command:
            if isinstance(x, str):
                result.append(x)
            else:
                result.append(box_file_paths[x])
        return result


COMPILE_JOB_LIMITS = IsolateLimits(
    time_limit_ms=10000,
    wall_time_ms=10000,
    memory_limit_mb=1024,
    cg_memory_limit_kb=1024 * 256,
    file_size_limit_kb=1024 * 16,
)


def _output_binary_name(command: list[str | int]) -> str | None:
    for index, arg in enumerate(command):
        if arg == "-o" and index + 1 < len(command) and isinstance(command[index + 1], str):
            return command[index + 1]
    return None


class CompileJobResult(StorageModel):
    stdout_digest: str
    stderr_digest: str
    meta: IsolateMeta
    program_digest: str | None = None


class CompileJob(AbstractJob):
    job_type = "compile"

    def get_description(self) -> str:
        return "Compile source files into a binary"

    def get_cache_policy(self) -> CachePolicy:
        return CachePolicy.Never

    @classmethod
    def make_input_digest(
        cls,
        storage: AbstractStorage,
        *,
        sources: list[tuple[Path | bytes, str | Path]],
        command: list[str | int],
    ) -> str:
        file_digests: list[str] = []
        file_paths: list[str] = []
        seen: set[str] = set()
        for source, sandbox_path in sources:
            rel = sandbox_relpath(sandbox_path, reserved=COMPILE_RESERVED_PATHS)
            if rel in seen:
                raise ValueError(f"duplicate sandbox path: {rel}")
            seen.add(rel)
            if isinstance(source, bytes):
                file_digests.append(storage.upload_bytes(source))
            else:
                file_digests.append(storage.upload_file(source))
            file_paths.append(rel)
        command = validate_compile_command(command, len(file_paths))
        return cls._upload_input(storage, {
            "files_digests": file_digests,
            "files_paths": file_paths,
            "command": command,
        })

    def _parse_arguments(self, context: JobContext) -> CompileJobArguments:
        arguments = context.storage.download_dictionary(context.input_digest)
        if arguments is None:
            raise ValueError("missing compile job input")
        self._require_job_type(arguments)
        file_digests = list(arguments["files_digests"])
        file_paths = [
            Path(sandbox_relpath(path, reserved=COMPILE_RESERVED_PATHS))
            for path in arguments["files_paths"]
        ]
        if len(file_digests) != len(file_paths):
            raise ValueError("files_digests and files_paths length mismatch")
        command = validate_compile_command(list(arguments["command"]), len(file_paths))
        for digest in file_digests:
            context.storage.validate_digest(digest)
        return CompileJobArguments(
            file_digests=file_digests,
            file_paths=file_paths,
            command=command,
        )

    def get_result(self, storage: AbstractStorage) -> CompileJobResult | None:
        digest = self.get_output_digest()
        if digest is None:
            return None
        return CompileJobResult.from_storage(storage, digest)

    def _execute(self, context: JobContext) -> str | None:
        arguments = self._parse_arguments(context)
        storage = context.storage
        if context.isolate_factory is None:
            raise RuntimeError("isolate_factory is required for CompileJob")

        with context.isolate_factory.get_sandbox() as sandbox:
            box_args = []
            for file_digest, file_path in zip(arguments.file_digests, arguments.file_paths):
                box_file_path = sandbox.box_path(file_path)
                context.storage.download_file(file_digest, box_file_path)
                box_args.append(sandbox.box_arg(box_file_path))

            compile_stdout_local_path = 'compile_stdout.txt'
            compile_stderr_local_path = 'compile_stderr.txt'

            args = arguments.to_cli_arguments(box_args)
            meta = sandbox.run(
                args,
                COMPILE_JOB_LIMITS,
                COMPILE_PROFILE,
                stdout_file=compile_stdout_local_path,
                stderr_file=compile_stderr_local_path,
            )

            stdout_digest = storage.upload_file(sandbox.box_path(compile_stdout_local_path))
            stderr_digest = storage.upload_file(sandbox.box_path(compile_stderr_local_path))

            program_digest = None
            output_name = _output_binary_name(arguments.command)
            if output_name is not None:
                sandbox_relpath(output_name, reserved=COMPILE_RESERVED_PATHS)
            if output_name is not None and meta.is_successful_termination():
                output_path = sandbox.box_path(output_name)
                if output_path.is_file():
                    program_digest = storage.upload_file(output_path)

            result = CompileJobResult(
                stdout_digest=stdout_digest,
                stderr_digest=stderr_digest,
                meta=meta,
                program_digest=program_digest,
            )
            self.job_result = result.upload(storage)

            if meta.is_sandbox_error():
                self.job_status = JobStatus.FAILED
                self.job_failure_reason = "Sandbox run failed"
                return None

            if not meta.is_successful_termination():
                self.job_status = JobStatus.FAILED
                self.job_failure_reason = "Compilation failed"
                return None

            return self.job_result


class SolutionCompileJob(CompileJob):
    job_type = "solution_compile"

    def get_description(self) -> str:
        return "Compile a participant solution"

    def get_cache_policy(self) -> CachePolicy:
        return CachePolicy.SolutionCompile


class ModelSolutionCompileJob(CompileJob):
    job_type = "model_solution_compile"

    def get_description(self) -> str:
        return "Compile a model solution"

    def get_cache_policy(self) -> CachePolicy:
        return CachePolicy.ModelSolutionCompile


class CheckerCompileJob(CompileJob):
    job_type = "checker_compile"

    def get_description(self) -> str:
        return "Compile a checker"

    def get_cache_policy(self) -> CachePolicy:
        return CachePolicy.CheckerCompile


class ValidatorCompileJob(CompileJob):
    job_type = "validator_compile"

    def get_description(self) -> str:
        return "Compile a validator"

    def get_cache_policy(self) -> CachePolicy:
        return CachePolicy.ValidatorCompile


class GeneratorCompileJob(CompileJob):
    job_type = "generator_compile"

    def get_description(self) -> str:
        return "Compile a generator"

    def get_cache_policy(self) -> CachePolicy:
        return CachePolicy.GeneratorCompile
