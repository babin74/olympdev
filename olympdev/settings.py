"""Host-local configuration for olympdev.

Everything a deployment may want to tune lives here: sandbox limits caps, the
isolate command and its box pool, storage locations and compiler toolchains.
Settings are read once from a TOML file found through a search chain and are
reachable from anywhere via :func:`get_settings`.

This module deliberately imports nothing from ``olympdev`` so that any other
module (including ``schema`` and ``storage``) may depend on it.
"""

from __future__ import annotations

import logging
import os
import shutil
import stat
import subprocess
import tempfile
import tomllib
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from enum import StrEnum
from pathlib import Path
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, model_validator

CONFIG_ENV_VAR = "OLYMPDEV_CONFIG"
PROJECT_CONFIG_NAME = "olympdev.toml"
USER_CONFIG_PATH = Path.home() / ".config" / "olympdev" / "config.toml"
SYSTEM_CONFIG_PATH = Path("/etc/olympdev/config.toml")


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Language(StrEnum):
    CPP = "cpp"
    C = "c"


SOURCE_SUFFIX_LANGUAGES: dict[str, Language] = {
    ".c": Language.C,
    ".cc": Language.CPP,
    ".cpp": Language.CPP,
    ".cxx": Language.CPP,
}


def language_for_source(path: Path | str) -> Language:
    suffix = Path(path).suffix.lower()
    try:
        return SOURCE_SUFFIX_LANGUAGES[suffix]
    except KeyError:
        raise ValueError(f"unsupported source suffix: {suffix!r}") from None


def check_limit_range(name: str, value: int, *, minimum: int, maximum: int) -> None:
    if value < minimum or value > maximum:
        raise ValueError(f"{name} must be in {minimum}..{maximum}, got {value}")


class LimitCaps(_Frozen):
    """Upper bounds every problem and job limit is checked against.

    These are the ceiling of what this host will ever run, not the limits of a
    particular problem. The host that *validates* is authoritative: a problem
    built where caps are wider is rejected here.
    """

    max_time_limit_ms: int = 300_000
    max_wall_time_ms: int = 600_000
    max_memory_limit_mb: int = 4096
    max_file_size_limit_kb: int = 256 * 1024
    max_extra_time_ms: int = 10_000
    max_tests_per_problem: int = 1024
    max_test_file_bytes: int = 32 * 1024 * 1024
    max_dictionary_bytes: int = 1_048_576

    @property
    def max_aspace_limit_kb(self) -> int:
        return self.max_memory_limit_mb * 1024

    @property
    def max_cg_memory_limit_kb(self) -> int:
        return self.max_memory_limit_mb * 1024

    @model_validator(mode="after")
    def _check_positive(self) -> Self:
        for name in type(self).model_fields:
            value = getattr(self, name)
            if value < 1:
                raise ValueError(f"{name} must be positive, got {value}")
        return self


MAX_BOX_ID = 999


class IsolateSettings(_Frozen):
    """How to reach isolate and how many boxes this host may use."""

    cmd: str = "isolate"
    lock_dir: Path | None = None
    sandbox_count: int = 1
    first_box_id: int = 1
    # isolate --init -q blocks,inodes (1 KiB blocks on ext). Dropped if the
    # filesystem has no quota support.
    quota_blocks: int = 256 * 1024
    quota_inodes: int = 4096
    use_cgroups: bool = True

    @model_validator(mode="after")
    def _check_pool(self) -> Self:
        if self.sandbox_count < 1:
            raise ValueError("sandbox_count must be positive")
        if self.first_box_id < 0:
            raise ValueError("first_box_id must be non-negative")
        if self.first_box_id + self.sandbox_count > MAX_BOX_ID + 1:
            raise ValueError(
                f"box ids {self.first_box_id}.."
                f"{self.first_box_id + self.sandbox_count - 1} exceed "
                f"the isolate maximum of {MAX_BOX_ID}"
            )
        if self.quota_blocks < 0 or self.quota_inodes < 0:
            raise ValueError("quota values must be non-negative")
        return self

    def resolved_lock_dir(self) -> Path:
        if self.lock_dir is not None:
            return self.lock_dir
        return Path(tempfile.gettempdir()) / "olympdev-isolate-locks"


class CompileLimits(_Frozen):
    """Limits for the compile job itself.

    These belong to the host, not to a problem: a participant never chooses
    them. They are still checked against LimitCaps, so a host that caps memory
    below what its compiler needs is a configuration error - and one reported
    when compiling, not at import time.
    """

    time_limit_ms: int = 10_000
    wall_time_ms: int = 10_000
    memory_limit_mb: int = 1024
    cg_memory_limit_kb: int = 1024 * 256
    file_size_limit_kb: int = 1024 * 16


class StorageSettings(_Frozen):
    """Where content-addressed blobs live on this host."""

    root: Path = Path.home() / ".cache" / "olympdev" / "storage"
    # When set, `root` is treated as a local cache in front of this backend.
    remote_root: Path | None = None


class ToolchainSettings(_Frozen):
    """How to compile one language.

    ``compiler`` is resolved through PATH when it is not an absolute path.
    ``allowed_flags``, when set, replaces the built-in denylist with a strict
    allowlist: only these flags may appear in a compile command.
    """

    compiler: str | None = None
    flags: list[str] = ["-O2"]
    allowed_flags: list[str] | None = None

    def resolve_compiler(self, language: Language) -> str:
        if self.compiler is not None:
            if Path(self.compiler).is_absolute():
                return self.compiler
            found = shutil.which(self.compiler)
            if found is None:
                raise RuntimeError(f"compiler not found in PATH: {self.compiler}")
            return found
        names = ("gcc", "clang", "cc") if language is Language.C else (
            "g++", "clang++", "c++"
        )
        for name in names:
            found = shutil.which(name)
            if found is not None:
                return found
        raise RuntimeError(f"no compiler found for {language.value} (tried {names})")


DEFAULT_TOOLCHAINS: dict[Language, ToolchainSettings] = {
    Language.CPP: ToolchainSettings(),
    Language.C: ToolchainSettings(),
}


class StatementEngine(StrEnum):
    """Which typesetting system a statement is written for.

    Only LaTeX is implemented today; a problem's ``engine`` field and this
    settings machinery are already shaped so a ``TYPST`` member is additive -
    no pipeline or job changes needed to add it later.
    """

    LATEX = "latex"


_DEFAULT_STATEMENT_COMMANDS: dict[StatementEngine, str] = {
    StatementEngine.LATEX: "latexmk",
}

#: Asking TeX where its cache lives is a local lookup; it should never hang.
_KPSEWHICH_TIMEOUT_SEC = 5.0

_logger = logging.getLogger(__name__)
_warned: set[str] = set()


def _note_once(message: str, *, level: int = logging.WARNING) -> None:
    """Host facts do not change between runs; saying them once is enough."""
    if message not in _warned:
        _warned.add(message)
        _logger.log(level, "%s", message)


def _readable_by_another_user(path: Path) -> bool:
    """Whether a process running as a different user could read this directory.

    isolate runs sandboxed processes under its own unprivileged user. A bind
    mount is made by root and succeeds regardless, but reads through it are
    still checked against the directory's own mode - which is why a cache
    living in a private home directory (mode 750) is invisible inside.
    """
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError:
        return False
    return mode & 0o005 == 0o005


class StatementToolchainSettings(_Frozen):
    """How to compile one statement engine's source into a PDF.

    ``command`` is resolved through PATH when it is not an absolute path,
    same as ``ToolchainSettings.compiler``.
    """

    command: str | None = None
    args: list[str] = ["-pdf", "-interaction=nonstopmode", "-halt-on-error"]
    #: TeX's generated-font cache, bound read-only into the statement
    #: sandbox so Cyrillic (T2A/LH) metrics generated on the host are
    #: readable inside it. Unset asks TeX itself where it keeps them; an
    #: empty string mounts nothing.
    texmf_var: Path | str | None = None

    def resolve_texmf_var(self) -> Path | None:
        """The host's ``$TEXMFVAR``, when the sandbox could actually read it.

        A cache that the sandbox cannot read is worse than no cache: it
        mounts, TeX finds nothing, and the failure blames a font. So an
        unreadable directory is reported here and not mounted at all.
        """
        explicit = self.texmf_var is not None
        if explicit:
            if str(self.texmf_var) == "":
                return None
            path = Path(self.texmf_var).expanduser()
        else:
            probed = self._probe_texmf_var()
            if probed is None:
                return None
            path = probed
        if not path.is_dir():
            if explicit:
                _note_once(f"statement texmf_var is not a directory: {path}")
            return None
        if not _readable_by_another_user(path):
            # Only statements that need generated fonts care, and the ones
            # that do say so themselves when they fail - so this is a note,
            # not a warning on every English build.
            _note_once(
                f"TeX's font cache at {path} is not readable by the sandbox "
                "user, so Cyrillic statements cannot find their metrics. "
                f"Run `chmod -R a+rX {path}`; only that directory is bound "
                "into the sandbox, so the ones above it stay as they are.",
                level=logging.INFO,
            )
            return None
        return path

    @staticmethod
    def _probe_texmf_var() -> Path | None:
        """Ask TeX itself where it caches generated fonts."""
        kpsewhich = shutil.which("kpsewhich")
        if kpsewhich is None:
            return None
        try:
            found = subprocess.run(
                [kpsewhich, "-var-value=TEXMFVAR"],
                capture_output=True,
                text=True,
                timeout=_KPSEWHICH_TIMEOUT_SEC,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if found.returncode != 0 or not found.stdout.strip():
            return None
        return Path(found.stdout.strip()).expanduser()

    def resolve_command(self, engine: StatementEngine) -> str:
        name = self.command or _DEFAULT_STATEMENT_COMMANDS[engine]
        if Path(name).is_absolute():
            return name
        found = shutil.which(name)
        if found is None:
            raise RuntimeError(f"statement compiler not found in PATH: {name}")
        return found


DEFAULT_STATEMENT_TOOLCHAINS: dict[StatementEngine, StatementToolchainSettings] = {
    StatementEngine.LATEX: StatementToolchainSettings(),
}


class Settings(_Frozen):
    limits: LimitCaps = LimitCaps()
    compile: CompileLimits = CompileLimits()
    isolate: IsolateSettings = IsolateSettings()
    storage: StorageSettings = StorageSettings()
    toolchains: dict[Language, ToolchainSettings] = DEFAULT_TOOLCHAINS
    statement_toolchains: dict[StatementEngine, StatementToolchainSettings] = (
        DEFAULT_STATEMENT_TOOLCHAINS
    )
    # Where these settings came from; None means built-in defaults.
    source_path: Path | None = None

    def toolchain(self, language: Language) -> ToolchainSettings:
        return self.toolchains.get(language, ToolchainSettings())

    def toolchain_for_source(self, path: Path | str) -> ToolchainSettings:
        return self.toolchain(language_for_source(path))

    def statement_toolchain(self, engine: StatementEngine) -> StatementToolchainSettings:
        return self.statement_toolchains.get(engine, StatementToolchainSettings())


def config_search_paths() -> list[Path]:
    """Config locations in priority order. The first existing one wins."""
    paths: list[Path] = []
    override = os.getenv(CONFIG_ENV_VAR)
    if override:
        paths.append(Path(override))
    paths.append(Path.cwd() / PROJECT_CONFIG_NAME)
    paths.append(USER_CONFIG_PATH)
    paths.append(SYSTEM_CONFIG_PATH)
    return paths


def find_config_file() -> Path | None:
    override = os.getenv(CONFIG_ENV_VAR)
    for path in config_search_paths():
        if path.is_file():
            return path
    if override:
        # An explicit override that does not exist is a configuration error,
        # not a reason to silently fall back to defaults.
        raise FileNotFoundError(
            f"{CONFIG_ENV_VAR} points at a missing file: {override}"
        )
    return None


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with open(path, "rb") as handle:
            return tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"invalid TOML in {path}: {exc}") from None


def settings_from_mapping(
    data: Mapping[str, Any], *, source_path: Path | None = None
) -> Settings:
    return Settings.model_validate({**data, "source_path": source_path})


def load_settings(path: Path | None = None) -> Settings:
    """Load settings from ``path``, or from the search chain when omitted."""
    if path is None:
        path = find_config_file()
    if path is None:
        return Settings()
    return settings_from_mapping(_read_toml(path), source_path=path)


_settings: Settings | None = None


def get_settings() -> Settings:
    """The active settings, loaded once and cached for the process."""
    global _settings
    if _settings is None:
        _settings = load_settings()
    return _settings


def set_settings(settings: Settings | None) -> None:
    """Replace the active settings. ``None`` forces a reload on next access."""
    global _settings
    _settings = settings


@contextmanager
def using_settings(settings: Settings) -> Iterator[Settings]:
    """Temporarily activate ``settings`` (for tests and CLI overrides)."""
    global _settings
    previous = _settings
    _settings = settings
    try:
        yield settings
    finally:
        _settings = previous
