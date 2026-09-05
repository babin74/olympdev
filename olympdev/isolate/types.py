from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Self

from pydantic import field_validator, model_validator

from olympdev.schema import FrozenModel
from olympdev.settings import check_limit_range as _check_range, get_settings


class IsolateLimits(FrozenModel):
    time_limit_ms: int
    memory_limit_mb: int
    extra_time_ms: int = 500
    wall_time_ms: int | None = None
    aspace_limit_kb: int | None = None
    cg_memory_limit_kb: int | None = None
    file_size_limit_kb: int | None = None

    @model_validator(mode="after")
    def _check_ranges(self) -> Self:
        caps = get_settings().limits
        _check_range(
            "time_limit_ms", self.time_limit_ms,
            minimum=1, maximum=caps.max_time_limit_ms,
        )
        _check_range(
            "extra_time_ms", self.extra_time_ms,
            minimum=0, maximum=caps.max_extra_time_ms,
        )
        _check_range(
            "memory_limit_mb", self.memory_limit_mb,
            minimum=1, maximum=caps.max_memory_limit_mb,
        )
        optional = (
            ("wall_time_ms", self.wall_time_ms, caps.max_wall_time_ms),
            ("aspace_limit_kb", self.aspace_limit_kb, caps.max_aspace_limit_kb),
            (
                "cg_memory_limit_kb",
                self.cg_memory_limit_kb,
                caps.max_cg_memory_limit_kb,
            ),
            (
                "file_size_limit_kb",
                self.file_size_limit_kb,
                caps.max_file_size_limit_kb,
            ),
        )
        for name, value, maximum in optional:
            if value is not None:
                _check_range(name, value, minimum=1, maximum=maximum)
        return self

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> IsolateLimits:
        return cls(
            time_limit_ms=int(data["time_limit_ms"]),
            memory_limit_mb=int(data["memory_limit_mb"]),
            wall_time_ms=int(data["wall_time_ms"]),
            aspace_limit_kb=(
                None
                if data.get("aspace_limit_kb") is None
                else int(data["aspace_limit_kb"])
            ),
            cg_memory_limit_kb=int(data["cg_memory_limit_kb"]),
            file_size_limit_kb=int(data["file_size_limit_kb"]),
        )

    def memory_kb(self) -> int:
        return self.memory_limit_mb * 1024

    def aspace_kb(self) -> int:
        if self.aspace_limit_kb is not None:
            return self.aspace_limit_kb
        return self.memory_kb()

    def cg_memory_kb(self) -> int:
        if self.cg_memory_limit_kb is not None:
            return self.cg_memory_limit_kb
        return self.memory_kb()

    def time_sec(self) -> float:
        return self.time_limit_ms / 1000.0

    def extra_kill_sec(self) -> float:
        """Absolute CPU time for isolate `-x` (limit + extra)."""
        return (self.time_limit_ms + self.extra_time_ms) / 1000.0

    def wall_sec(self) -> float:
        wall_ms = (
            self.time_limit_ms * 2 if self.wall_time_ms is None else self.wall_time_ms
        )
        return wall_ms / 1000.0


class IsolateQuota(FrozenModel):
    """Disk quota for `isolate --init -q blocks,inodes`.

    isolate applies this at box creation, not at `--run`. Both values are
    required together. `blocks` is filesystem blocks (typically 1 KiB on ext);
    `inodes` is the file-count cap. Requires quota support on the box
    filesystem (`box_root` in isolate.cfg). Omit the object to leave quota unset.
    """

    blocks: int
    inodes: int

    @model_validator(mode="after")
    def _check_non_negative(self) -> Self:
        if self.blocks < 0:
            raise ValueError("blocks must be non-negative")
        if self.inodes < 0:
            raise ValueError("inodes must be non-negative")
        return self

    def to_cli(self) -> str:
        return f"{self.blocks},{self.inodes}"

# isolate meta keys -> IsolateMeta field names
_META_KEY_MAP = {
    "cg-mem": "cg_memory_kb",
    "cg-oom-killed": "cg_oom_killed",
    "csw-forced": "csw_forced",
    "csw-voluntary": "csw_voluntary",
    "exitcode": "exitcode",
    "exitsig": "exitsig",
    "killed": "killed",
    "max-rss": "max_rss_kb",
    "message": "message",
    "status": "status",
    "time": "time",
    "time-wall": "time_wall",
}

_BOOL_KEYS = {"cg_oom_killed", "killed"}
_INT_KEYS = {
    "cg_memory_kb", "csw_forced", "csw_voluntary",
    "exitcode", "exitsig", "max_rss_kb",
}
_FLOAT_KEYS = {"time", "time_wall"}

# isolate meta status codes (see isolate.1):
# OK - normal termination (absent in meta file is treated as OK)
# RE - run-time error, non-zero exit code
# SG - program died on a signal
# TO - timed out
# XX - internal sandbox error
_SANDBOX_ERROR_STATUSES = frozenset({"XX"})


class IsolateMeta(FrozenModel):
    # Total memory use by the whole control group (in kilobytes).
    # Repeated --run in the same box can include page cache from earlier runs.
    cg_memory_kb: int = 0
    # Present when the program was killed by the out-of-memory killer
    cg_oom_killed: bool = False
    csw_forced: int = 0         # Number of context switches forced by the kernel
    # Number of context switches caused by the process giving up the CPU voluntarily
    csw_voluntary: int = 0
    exitcode: int = 0           # The program has exited normally with this exit code
    exitsig: int = 0            # The program has exited after receiving this fatal signal
    killed: bool = False        # Present when the program was terminated by the sandbox
    # Maximum resident set size of the process (in kilobytes)
    max_rss_kb: int = 0
    message: str = ""           # Status message; absent on success
    # Two-letter status code; absent means success ("OK")
    status: str = "OK"
    time: float = 0.0           # Run time of the program in fractional seconds
    time_wall: float = 0.0      # Wall clock time of the program in fractional seconds

    def is_sandbox_error(self) -> bool:
        return self.status in _SANDBOX_ERROR_STATUSES

    def is_successful_termination(self) -> bool:
        return self.status == "OK" and self.exitcode == 0

    @classmethod
    def from_meta_file(cls, meta_path: Path) -> IsolateMeta:
        raw: dict[str, str] = {}
        text = meta_path.read_text()
        for line in text.splitlines():
            key, sep, value = line.partition(":")
            if sep:
                raw[key.strip()] = value.strip()

        fields: dict[str, object] = {}
        for meta_key, field_name in _META_KEY_MAP.items():
            if meta_key not in raw:
                continue
            value = raw[meta_key]
            try:
                if field_name in _BOOL_KEYS:
                    # Presence alone means true; value may be empty or "1"
                    fields[field_name] = True
                elif field_name in _INT_KEYS:
                    fields[field_name] = int(value)
                elif field_name in _FLOAT_KEYS:
                    fields[field_name] = float(value)
                else:
                    fields[field_name] = value
            except ValueError as exc:
                raise ValueError(
                    f"invalid isolate meta value for {meta_key!r}: {value!r}"
                ) from exc
        return cls(**fields)


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