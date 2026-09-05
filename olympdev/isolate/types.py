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
