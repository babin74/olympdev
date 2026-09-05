"""Parsing and rendering of isolate's ``--dir`` grammar.

Private on purpose: mounts are chosen by the sandbox profiles in
``olympdev.isolate.profiles``, never by a caller or by anything derived from a
submission. Kept complete (and tested) so profiles can grow without redoing
the grammar work.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Self

from pydantic import field_validator, model_validator

from olympdev.schema import FrozenModel


# isolate --dir option names; colon-separated after in[=out]
_DIR_FS_TYPES = frozenset({"proc", "sysfs", "tmpfs", "devpts"})
_DIR_SPEC_SEPARATORS = frozenset({":", "="})


def _reject_dir_separators(label: str, value: str, original: Path) -> None:
    if any(sep in value for sep in _DIR_SPEC_SEPARATORS):
        raise ValueError(
            f"Invalid {label} (must not contain ':' or '='): {original}"
        )


def _dir_inside_cli(path: Path) -> str:
    """Path `in` inside the sandbox, as isolate parses it (no leading slash)."""
    parts = [part for part in path.as_posix().lstrip("/").split("/") if part and part != "."]
    if not parts or any(part == ".." for part in parts):
        raise ValueError(f"Invalid path in sandbox: {path}")
    inside = "/".join(parts)
    _reject_dir_separators("path in sandbox", inside, path)
    return inside


def _dir_outside_cli(path: Path) -> str:
    """Path `out` as seen by the caller. isolate requires '/' or './'."""
    posix = path.as_posix()
    if posix.startswith("/"):
        _reject_dir_separators("path on host", posix, path)
        return posix
    if posix in {"", "."}:
        raise ValueError(f"Invalid path on host: {path}")
    outside = posix if posix.startswith("./") else f"./{posix}"
    _reject_dir_separators("path on host", outside, path)
    return outside


def _replace_dir_rules(
    rules: Sequence[IsolateDirectoryRule | IsolateRemoveDirectoryRule],
) -> list[IsolateDirectoryRule | IsolateRemoveDirectoryRule]:
    """Replace a rule for the same `in` in place, keeping its original position."""
    result: list[IsolateDirectoryRule | IsolateRemoveDirectoryRule] = []
    index_by_inside: dict[str, int] = {}
    for rule in rules:
        key = _dir_inside_cli(rule.path_in_sandbox)
        if key in index_by_inside:
            result[index_by_inside[key]] = rule
        else:
            index_by_inside[key] = len(result)
            result.append(rule)
    return result


class IsolateDirectoryRule(FrozenModel):
    path_in_sandbox: Path
    path_on_host: Path | None = None

    write_access: bool = False     # rw: allow read-write access (default is read-only)
    device_access: bool = False    # dev: allow access to character and block devices
    noexec: bool = False           # noexec: disallow execution of binaries
    ignore_missing: bool = False   # maybe: ignore the rule if the bound directory does not exist
    # fs: mount a device-less filesystem named `fs` instead of binding a directory.
    # Supported: proc, sysfs, tmpfs, devpts. `path_on_host` must be None.
    fs: str = ""
    # tmp: bind a freshly created temporary directory. Accepts no out, implies rw.
    tmp_dir: bool = False
    # Recursive bind is the default; False emits norec.
    recursive_bind: bool = True

    @model_validator(mode="after")
    def _check_correctness(self) -> Self:
        self.ensure_correctness()
        return self

    def ensure_correctness(self) -> None:
        _dir_inside_cli(self.path_in_sandbox)
        if self.fs and self.fs not in _DIR_FS_TYPES:
            raise ValueError(f"Invalid filesystem: {self.fs}")
        if self.fs and self.tmp_dir:
            raise ValueError("Flags 'fs' and 'tmp' are mutually exclusive")
        if self.tmp_dir and self.path_on_host is not None:
            raise ValueError("Temporary directory mounts do not have a right-hand side")
        if self.fs and self.path_on_host is not None:
            raise ValueError("Filesystem mounts use fs as 'out'; path_on_host must be None")
        if self.path_on_host is not None:
            _dir_outside_cli(self.path_on_host)

    def to_cli(self) -> str:
        """A single `--dir=in[=out][:options]` argument, as isolate expects it."""
        inside = _dir_inside_cli(self.path_in_sandbox)
        options: list[str] = []
        # tmp implies rw; isolate sets DIR_FLAG_RW itself, so do not emit a redundant rw.
        if self.write_access and not self.tmp_dir:
            options.append("rw")
        if self.device_access:
            options.append("dev")
        if self.noexec:
            options.append("noexec")
        if self.ignore_missing:
            options.append("maybe")
        if self.fs:
            options.append("fs")
        if self.tmp_dir:
            options.append("tmp")
        if not self.recursive_bind:
            options.append("norec")
        suffix = f":{':'.join(options)}" if options else ""

        if self.tmp_dir:
            spec = inside
        elif self.fs:
            spec = f"{inside}={self.fs}"
        elif self.path_on_host is None:
            # --dir=dir[:options] binds /dir to dir inside the sandbox
            spec = inside
        else:
            spec = f"{inside}={_dir_outside_cli(self.path_on_host)}"
        return f"--dir={spec}{suffix}"


class IsolateRemoveDirectoryRule(FrozenModel):
    path_in_sandbox: Path

    @model_validator(mode="after")
    def _check_path(self) -> Self:
        _dir_inside_cli(self.path_in_sandbox)
        return self

    def to_cli(self) -> str:
        return f"--dir={_dir_inside_cli(self.path_in_sandbox)}="


class IsolateDirectoryRules(FrozenModel):
    allow_default_directories: bool = True
    rules: tuple[IsolateDirectoryRule | IsolateRemoveDirectoryRule, ...] = ()

    @field_validator("rules", mode="after")
    @classmethod
    def _dedupe_rules(
        cls,
        rules: Sequence[IsolateDirectoryRule | IsolateRemoveDirectoryRule],
    ) -> tuple[IsolateDirectoryRule | IsolateRemoveDirectoryRule, ...]:
        return tuple(_replace_dir_rules(rules))

    def to_cli(self) -> list[str]:
        args: list[str] = []
        if not self.allow_default_directories:
            args.append("--no-default-dirs")
        args.extend(rule.to_cli() for rule in self.rules)
        return args