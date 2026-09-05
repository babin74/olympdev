"""Host-local configuration for olympdev.

Everything a deployment may want to tune lives here: sandbox limits caps, the
isolate command and its box pool, storage locations and compiler toolchains.
Settings are read once from a TOML file found through a search chain and are
reachable from anywhere via :func:`get_settings`.

This module deliberately imports nothing from ``olympdev`` so that any other
module (including ``schema`` and ``storage``) may depend on it.
"""

from __future__ import annotations

import os
import shutil
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


class Settings(_Frozen):
    limits: LimitCaps = LimitCaps()
    isolate: IsolateSettings = IsolateSettings()
    storage: StorageSettings = StorageSettings()
    toolchains: dict[Language, ToolchainSettings] = DEFAULT_TOOLCHAINS
    # Where these settings came from; None means built-in defaults.
    source_path: Path | None = None

    def toolchain(self, language: Language) -> ToolchainSettings:
        return self.toolchains.get(language, ToolchainSettings())

    def toolchain_for_source(self, path: Path | str) -> ToolchainSettings:
        return self.toolchain(language_for_source(path))


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
