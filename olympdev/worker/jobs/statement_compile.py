"""Compiling a rendered statement source into a PDF, in the sandbox.

Mirrors ``worker/jobs/compile.py``'s ``CompileJob`` closely, but the output
is a named file the command is expected to produce (``output_relpath``)
rather than a binary named by an ``-o`` flag, and the allowed commands are
statement typesetters (``latexmk`` and friends) instead of C/C++ compilers.
The compiler and its flags come only from settings (operator-trusted, never
from a problem's info.json), so - unlike ``CompileJob`` - there is no
per-flag allow/deny-list here: a problem author only controls the body text
and sample files that get compiled, never the command that compiles them.
"""

import os
import re
from pathlib import Path

from olympdev.isolate import (
    COMPILE_RESERVED_PATHS,
    IsolateMeta,
    sandbox_relpath,
    statement_profile,
)
from olympdev.schema import FrozenModel, StorageModel
from olympdev.storage import AbstractStorage
from olympdev.worker.cache.policy import CachePolicy
from olympdev.worker.job import AbstractJob, JobContext, JobStatus
from olympdev.worker.jobs.compile import compile_job_limits
from olympdev.worker.jobs.sandbox import resolve_sandbox_files

_STATEMENT_COMPILER_NAME = re.compile(r"^(?:latexmk|pdflatex|xelatex|lualatex)$")
_STATEMENT_COMPILER_DIRS = (
    Path("/usr/bin"),
    Path("/bin"),
    Path("/usr/local/bin"),
)
#: install-tl.sh (TeX Live's own upstream installer, distinct from a
#: distro's packaged texlive-*) lays binaries out as
#: /usr/local/texlive/<year>/bin/<arch>/ - a standard, common layout, not a
#: one-off. latexmk there is itself a symlink into texmf-dist/scripts/, so
#: this directory check must run on the path as given, never on
#: path.resolve() - resolving first would validate the symlink's target
#: directory instead of the "public" bin directory this pattern names.
_TEXLIVE_BIN_DIR = re.compile(r"^/usr/local/texlive/[^/]+/bin/[^/]+$")

#: Log tail kept on a failed compile, for a useful failure_reason.
_LOG_TAIL_BYTES = 4096

#: kpathsea asking for a font it could not find and cannot build here. The
#: sandbox has no writable TEXMFVAR and no mktextfm, so this never resolves
#: itself - it is a host provisioning problem, and saying so beats a raw
#: latexmk tail that mentions a font name and nothing else.
_MISSING_FONT = re.compile(r"mktextfm|mktexpk|Font .* not loadable")
_MISSING_FONT_HINT = (
    "a font metric is missing and cannot be generated inside the sandbox "
    "(Cyrillic statements need the LH/T2A metrics). Two things fix this, both "
    "on the host: generate the metric once - `mktextfm <name>`, or "
    "`tlmgr install lh` - and make TeX's font cache readable by the sandbox "
    "user, which runs as nobody, not as you: "
    "`chmod -R a+rX \"$(kpsewhich -var-value=TEXMFVAR)\"`. Only the cache "
    "itself needs it - the sandbox binds that directory, so the modes of the "
    "directories above it do not matter and your home stays private"
)


def _read_tail(path: Path) -> str:
    """The last few KiB of a file, or '' when it was never written."""
    if not path.is_file():
        return ""
    return path.read_bytes()[-_LOG_TAIL_BYTES:].decode("utf-8", errors="replace")


def _compiler_dir_allowed(path: Path) -> bool:
    # Lexical normalization only (collapses ``..``/``.``) - deliberately not
    # path.resolve(), which would follow a symlinked compiler (e.g.
    # TeX Live's latexmk) to its target directory instead of checking the
    # directory the operator's settings actually name.
    parent = Path(os.path.normpath(str(path))).parent
    for allowed in _STATEMENT_COMPILER_DIRS:
        try:
            if parent == allowed or parent.is_relative_to(allowed):
                return True
        except ValueError:
            continue
    return bool(_TEXLIVE_BIN_DIR.fullmatch(parent.as_posix()))


def validate_statement_compile_command(
    command: list[str | int],
    file_count: int,
    *,
    reserved: frozenset[str] = COMPILE_RESERVED_PATHS,
) -> list[str | int]:
    if not command:
        raise ValueError("statement compile command must not be empty")
    compiler = command[0]
    if not isinstance(compiler, str):
        raise ValueError("statement compile command[0] must be an absolute path")
    compiler_path = Path(compiler)
    if not compiler_path.is_absolute():
        raise ValueError(f"statement compiler path must be absolute: {compiler!r}")
    if not _STATEMENT_COMPILER_NAME.fullmatch(compiler_path.name):
        raise ValueError(f"statement compiler is not allowed: {compiler_path.name!r}")
    if not _compiler_dir_allowed(compiler_path):
        raise ValueError(
            f"statement compiler path is not in an allowed directory: {compiler}"
        )
    if not compiler_path.is_file():
        raise ValueError(f"statement compiler does not exist: {compiler}")

    validated: list[str | int] = [compiler]
    for arg in command[1:]:
        if isinstance(arg, bool) or not isinstance(arg, (str, int)):
            raise ValueError(f"invalid statement compile argument: {arg!r}")
        if isinstance(arg, int):
            if arg < 0 or arg >= file_count:
                raise ValueError(f"statement compile command index out of range: {arg}")
            validated.append(arg)
            continue
        if arg == "":
            raise ValueError("empty statement compile argument")
        validated.append(arg)
    return validated


class StatementCompileJobArguments(FrozenModel):
    file_digests: list[str]
    file_paths: list[Path]
    command: list[str | int]
    output_relpath: Path

    def to_cli_arguments(self, box_file_paths: list[str]) -> list[str]:
        result = []
        for x in self.command:
            if isinstance(x, str):
                result.append(x)
            else:
                result.append(box_file_paths[x])
        return result


class StatementCompileResult(StorageModel):
    stdout_digest: str
    stderr_digest: str
    meta: IsolateMeta
    pdf_digest: str | None = None
    #: TeX's own .log. This is where an error names the .tex file and the
    #: line; stdout and stderr only carry latexmk's summary of it.
    log_digest: str | None = None


class StatementCompileJob(AbstractJob):
    job_type = "statement_compile"

    def get_description(self) -> str:
        return "Compile a statement source into a PDF"

    def get_cache_policy(self) -> CachePolicy:
        return CachePolicy.StatementCompile

    @classmethod
    def make_input_digest(
        cls,
        storage: AbstractStorage,
        *,
        file_digests: list[tuple[str, str | Path]],
        command: list[str | int],
        output_relpath: str | Path,
    ) -> str:
        """Every source is already a digest - some staged by ``build.py``
        straight from disk, some produced by a just-finished job (a computed
        sample answer; see ``worker/pipelines/statement.py``). Either way
        this job only ever re-validates digests, never touches a host path.
        """
        resolved_digests, resolved_paths = resolve_sandbox_files(
            storage, file_digests=file_digests, reserved=COMPILE_RESERVED_PATHS
        )
        command = validate_statement_compile_command(command, len(resolved_paths))
        output_rel = sandbox_relpath(output_relpath, reserved=COMPILE_RESERVED_PATHS)
        return cls._upload_input(storage, {
            "files_digests": resolved_digests,
            "files_paths": resolved_paths,
            "command": command,
            "output_relpath": output_rel,
        })

    def _parse_arguments(self, context: JobContext) -> StatementCompileJobArguments:
        arguments = context.storage.download_dictionary(context.input_digest)
        if arguments is None:
            raise ValueError("missing statement compile job input")
        self._require_job_type(arguments)
        file_digests = list(arguments["files_digests"])
        file_paths = [
            Path(sandbox_relpath(path, reserved=COMPILE_RESERVED_PATHS))
            for path in arguments["files_paths"]
        ]
        if len(file_digests) != len(file_paths):
            raise ValueError("files_digests and files_paths length mismatch")
        command = validate_statement_compile_command(
            list(arguments["command"]), len(file_paths)
        )
        output_relpath = Path(
            sandbox_relpath(arguments["output_relpath"], reserved=COMPILE_RESERVED_PATHS)
        )
        for digest in file_digests:
            context.storage.validate_digest(digest)
        return StatementCompileJobArguments(
            file_digests=file_digests,
            file_paths=file_paths,
            command=command,
            output_relpath=output_relpath,
        )

    def get_result(self, storage: AbstractStorage) -> StatementCompileResult | None:
        digest = self.get_output_digest()
        if digest is None:
            return None
        return StatementCompileResult.from_storage(storage, digest)

    def _execute(self, context: JobContext) -> str | None:
        arguments = self._parse_arguments(context)
        storage = context.storage
        if context.isolate_factory is None:
            raise RuntimeError("isolate_factory is required for StatementCompileJob")

        with context.isolate_factory.get_sandbox() as sandbox:
            box_args = []
            for file_digest, file_path in zip(arguments.file_digests, arguments.file_paths):
                box_file_path = sandbox.box_path(file_path)
                context.storage.download_file(file_digest, box_file_path)
                box_args.append(sandbox.box_arg(box_file_path))

            stdout_local_path = "compile_stdout.txt"
            stderr_local_path = "compile_stderr.txt"

            args = arguments.to_cli_arguments(box_args)
            meta = sandbox.run(
                args,
                compile_job_limits(),
                statement_profile(),
                stdout_file=stdout_local_path,
                stderr_file=stderr_local_path,
            )

            stdout_digest = storage.upload_file(sandbox.box_path(stdout_local_path))
            stderr_digest = storage.upload_file(sandbox.box_path(stderr_local_path))

            pdf_digest = None
            output_path = sandbox.box_path(arguments.output_relpath)
            if meta.is_successful_termination() and output_path.is_file():
                pdf_digest = storage.upload_file(output_path)

            # TeX writes <job>.log beside its output, whether it succeeded or
            # not; on failure that log is the only place the error is spelled
            # out, so keep it addressable either way.
            log_path = sandbox.box_path(arguments.output_relpath.with_suffix(".log"))
            log_digest = storage.upload_file(log_path) if log_path.is_file() else None

            result = StatementCompileResult(
                stdout_digest=stdout_digest,
                stderr_digest=stderr_digest,
                meta=meta,
                pdf_digest=pdf_digest,
                log_digest=log_digest,
            )
            self.job_result = result.upload(storage)

            if meta.is_sandbox_error():
                self.job_status = JobStatus.FAILED
                self.job_failure_reason = "Sandbox run failed"
                return None

            if pdf_digest is None:
                self.job_status = JobStatus.FAILED
                # The .log first: TeX puts the .tex name and line number
                # there, while stderr usually holds only latexmk's summary.
                tail = (
                    _read_tail(log_path)
                    or _read_tail(sandbox.box_path(stdout_local_path))
                    or _read_tail(sandbox.box_path(stderr_local_path))
                )
                reason = f"Statement compilation failed: {tail}"
                if _MISSING_FONT.search(tail):
                    reason = f"Statement compilation failed: {_MISSING_FONT_HINT}\n{tail}"
                self.job_failure_reason = reason
                return None

            return self.job_result
