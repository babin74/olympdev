from __future__ import annotations

import fcntl
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Self

from olympdev.settings import get_settings
from olympdev.isolate.profiles import RUN_PROFILE, SandboxProfile
from olympdev.isolate.types import IsolateLimits, IsolateMeta, IsolateQuota

logger = logging.getLogger(__name__)

_INIT_CLEANUP_TIMEOUT_SEC = 30.0
_RUN_TIMEOUT_SLACK_SEC = 10.0
_NUM_BOXES = 1000
_DISCARD_SANDBOX_PATH = "/dev/null"
_LOCK_POLL_SEC = 0.05
_ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ISOLATE_STDERR_MAX = 8192

_box_thread_locks_guard = threading.Lock()
_box_thread_locks: dict[int, threading.Lock] = {}


def _thread_lock_for(box_id: int) -> threading.Lock:
    with _box_thread_locks_guard:
        lock = _box_thread_locks.get(box_id)
        if lock is None:
            lock = threading.Lock()
            _box_thread_locks[box_id] = lock
        return lock


def ensure_isolate_available() -> str:
    cmd = get_settings().isolate.cmd
    candidates: list[Path] = []
    given = Path(cmd)
    if given.is_absolute():
        candidates.append(given)
    else:
        candidates.append(Path("/usr/local/bin") / cmd)
        candidates.append(Path("/usr/bin") / cmd)
        found = shutil.which(cmd)
        if found:
            candidates.append(Path(found))
    seen: set[Path] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate.resolve())
    raise RuntimeError(f"isolate (cmd={cmd}) is not available")


def _lock_dir() -> Path:
    return get_settings().isolate.resolved_lock_dir()


def _build_init_args(quota: IsolateQuota | None = None) -> list[str]:
    args = ["--cg"]
    if quota is not None:
        args.extend(["-q", quota.to_cli()])
    args.append("--init")
    return args


def _build_run_args(
    limits: IsolateLimits,
    args: list[str],
    *,
    profile: SandboxProfile = RUN_PROFILE,
    stdin_arg: str | None = None,
    stdout_arg: str | None = None,
    stderr_arg: str | None = None,
    meta_path: str,
) -> list[str]:
    run_args = ["--cg"]
    run_args.extend(profile.dir_rules.to_cli())
    run_args.extend([
        "-t", f"{limits.time_sec():0.3f}",
        "-w", f"{limits.wall_sec():0.3f}",
        "-x", f"{limits.extra_kill_sec():0.3f}",
    ])
    run_args.extend(["-m", str(limits.aspace_kb())])
    run_args.extend(["--cg-mem", str(limits.cg_memory_kb())])
    if limits.file_size_limit_kb is not None:
        run_args.extend(["-f", str(limits.file_size_limit_kb)])
    run_args.append(f"--processes={profile.processes}")
    for key, value in profile.env.items():
        if not _ENV_KEY.fullmatch(key):
            raise ValueError(f"invalid environment variable name: {key!r}")
        if "\n" in value or "\x00" in value:
            raise ValueError(f"invalid environment value for {key}")
        run_args.append(f"--env={key}={value}")
    if stdin_arg is not None:
        run_args.extend(["-i", stdin_arg])
    if stdout_arg is not None:
        run_args.extend(["-o", stdout_arg])
    if stderr_arg is None:
        stderr_arg = _DISCARD_SANDBOX_PATH
    run_args.extend(["-r", stderr_arg])
    run_args.extend(["-M", meta_path, "--run", "--", *args])
    return run_args


class IsolateSandBox:
    _quota_supported: bool | None = None
    #: Boxes initialise concurrently, so without a lock every one of them
    #: discovers a quota-less filesystem separately and says so. The finding
    #: is a property of the host, not of the box: report it once.
    _quota_lock = threading.Lock()
    _quota_warned = False

    def __init__(
        self,
        box_id: int,
        *,
        quota: IsolateQuota | None = None,
        lock_timeout_sec: float | None = None,
    ) -> None:
        if not isinstance(box_id, int) or isinstance(box_id, bool):
            raise TypeError("box_id must be an int")
        if box_id < 0 or box_id >= _NUM_BOXES:
            raise ValueError(f"box_id must be in 0..{_NUM_BOXES - 1}")
        if lock_timeout_sec is not None and lock_timeout_sec < 0:
            raise ValueError("lock_timeout_sec must be non-negative")
        self.box_id = box_id
        self.quota = quota
        self.lock_timeout_sec = lock_timeout_sec
        self.isolate = ensure_isolate_available()
        self.box_root: Path | None = None
        self.box_dir: Path | None = None
        self._lock_fd: int | None = None
        self._thread_lock: threading.Lock | None = None

    def __enter__(self) -> Self:
        self._acquire_lock()
        try:
            self._init_box()
        except BaseException:
            self._release_lock()
            raise
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        try:
            self._cleanup_box()
        finally:
            self._release_lock()

    def _lock_path(self) -> Path:
        return _lock_dir() / f"box-{self.box_id}.lock"

    def _acquire_lock(self) -> None:
        tlock = _thread_lock_for(self.box_id)
        if self.lock_timeout_sec is None:
            tlock.acquire()
        elif not tlock.acquire(timeout=self.lock_timeout_sec):
            raise TimeoutError(
                f"timed out waiting for isolate box {self.box_id}"
            )
        self._thread_lock = tlock
        path = self._lock_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            if self.lock_timeout_sec is None:
                fcntl.flock(fd, fcntl.LOCK_EX)
            else:
                deadline = time.monotonic() + self.lock_timeout_sec
                while True:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError as exc:
                        if time.monotonic() >= deadline:
                            raise TimeoutError(
                                f"timed out waiting for isolate box {self.box_id}"
                            ) from exc
                        time.sleep(_LOCK_POLL_SEC)
        except BaseException:
            os.close(fd)
            self._thread_lock = None
            tlock.release()
            raise
        self._lock_fd = fd

    def _release_lock(self) -> None:
        fd = self._lock_fd
        tlock = self._thread_lock
        self._lock_fd = None
        self._thread_lock = None
        try:
            if fd is not None:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                finally:
                    os.close(fd)
        finally:
            if tlock is not None:
                tlock.release()

    def _init_box_path(self, box_root: Path | None) -> None:
        if box_root is None:
            self.box_root = None
            self.box_dir = None
            return
        self.box_root = box_root
        self.box_dir = box_root / "box"

    def _run_isolate(
        self,
        args: list[str],
        *,
        capture_stdout: bool = False,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess[str]:
        cmd = [self.isolate, f"--box-id={self.box_id}", *args]
        logger.debug("run: %s", " ".join(cmd))
        stderr_file = tempfile.SpooledTemporaryFile(
            max_size=_ISOLATE_STDERR_MAX, mode="w+"
        )
        try:
            try:
                result = subprocess.run(
                    cmd,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE if capture_stdout else subprocess.DEVNULL,
                    stderr=stderr_file,
                    text=True,
                    timeout=timeout,
                )
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError(
                    f"isolate timed out after {timeout}s: {' '.join(cmd)}"
                ) from exc
            stderr_file.seek(0)
            stderr_text = stderr_file.read(_ISOLATE_STDERR_MAX)
            return subprocess.CompletedProcess(
                result.args,
                result.returncode,
                result.stdout,
                stderr_text,
            )
        finally:
            stderr_file.close()

    def box_path(self, rel: str | Path) -> Path:
        """Resolve a path inside the sandbox working directory (host view)."""
        if self.box_dir is None:
            raise RuntimeError("sandbox is not initialized")
        path = Path(rel)
        if path.is_absolute():
            resolved = path.resolve()
        else:
            resolved = (self.box_dir / path).resolve()
        if not resolved.is_relative_to(self.box_dir.resolve()):
            raise ValueError(f"path {rel} is outside box_dir {self.box_dir}")
        return resolved

    def box_arg(self, rel: str | Path) -> str:
        """Path as seen inside the sandbox (/box is cwd)."""
        resolved = self.box_path(rel)
        return str(resolved.relative_to(self.box_dir.resolve()))

    def read_text(self, rel: str | Path) -> str:
        return self.box_path(rel).read_text()

    def write_text(self, rel: str | Path, text: str) -> Path:
        path = self.box_path(rel)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def run(
        self,
        args: list[str],
        limits: IsolateLimits,
        profile: SandboxProfile = RUN_PROFILE,
        *,
        stdin_file: str | Path | None = None,
        stdout_file: str | Path | None = None,
        stderr_file: str | Path | None = None,
    ) -> IsolateMeta:
        """Run a command inside the sandbox under a fixed profile.

        Without `stdout_file`, program stdout is discarded (isolate stdout is
        DEVNULL). Without `stderr_file`, program stderr goes to `/dev/null`
        inside the sandbox. Pass `stderr_file` to keep it.

        Processes, environment and mounts come from `profile` and cannot be
        widened per call.
        """
        if self.box_dir is None:
            raise RuntimeError("sandbox is not initialized")

        with tempfile.NamedTemporaryFile(
            prefix="isolate-meta-", suffix=".txt", delete=False
        ) as meta_file:
            meta_path = Path(meta_file.name)
        run_args = _build_run_args(
            limits,
            args,
            profile=profile,
            stdin_arg=(
                self.box_arg(stdin_file) if stdin_file is not None else None
            ),
            stdout_arg=(
                self.box_arg(stdout_file) if stdout_file is not None else None
            ),
            stderr_arg=(
                self.box_arg(stderr_file) if stderr_file is not None else None
            ),
            meta_path=str(meta_path.resolve()),
        )
        try:
            result = self._run_isolate(
                run_args,
                timeout=limits.wall_sec() + _RUN_TIMEOUT_SLACK_SEC,
            )
            logger.debug("Result: %s", result.returncode)
            logger.debug("Stderr: %s", result.stderr)

            # 0 = OK, 1 = program finished incorrectly (still has meta),
            # other = isolate internal error.
            if result.returncode not in (0, 1):
                details = (result.stderr or "").strip()
                raise RuntimeError(
                    f"isolate run failed (exit {result.returncode})"
                    + (f": {details}" if details else "")
                )

            if not meta_path.is_file():
                raise RuntimeError("isolate run produced no meta file")
            meta = IsolateMeta.from_meta_file(meta_path)
            if meta.status == "XX":
                details = meta.message or (result.stderr or "").strip()
                raise RuntimeError(
                    "isolate internal error (status XX)"
                    + (f": {details}" if details else "")
                )
            return meta
        finally:
            meta_path.unlink(missing_ok=True)

    def copy2(self, src: Path, dst: str | Path) -> Path:
        dst_path = self.box_path(dst)
        dst_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst_path)
        return dst_path

    def _init_box(self) -> None:
        quota = self.quota
        if quota is not None and IsolateSandBox._quota_supported is False:
            quota = None
        init_args = _build_init_args(quota)

        def attempt(args: list[str]) -> tuple[int, str]:
            result = self._run_isolate(
                args,
                capture_stdout=True,
                timeout=_INIT_CLEANUP_TIMEOUT_SEC,
            )
            if result.returncode == 0:
                self._init_box_path(Path(result.stdout.strip()))
            return result.returncode, result.stderr

        code, stderr = attempt(init_args)
        if code != 0:
            # Leftover box may have a different cgroup mode; try both cleanups.
            self._cleanup_isolate()
            code, stderr = attempt(init_args)
        if code != 0 and quota is not None:
            with IsolateSandBox._quota_lock:
                first = not IsolateSandBox._quota_warned
                IsolateSandBox._quota_warned = True
                IsolateSandBox._quota_supported = False
            if first:
                logger.warning(
                    "isolate quotas are unavailable on this filesystem, "
                    "continuing without them: %s",
                    (stderr or "").strip(),
                )
            init_args = _build_init_args(None)
            self._cleanup_isolate()
            code, stderr = attempt(init_args)
        elif code == 0 and quota is not None:
            IsolateSandBox._quota_supported = True
        if code != 0:
            raise RuntimeError(
                f"isolate init failed (exit {code})"
                + (f": {stderr}" if stderr else "")
            )

    def _cleanup_isolate(self) -> None:
        result = self._run_isolate(
            ["--cg", "--cleanup"],
            timeout=_INIT_CLEANUP_TIMEOUT_SEC,
        )
        if result.returncode != 0:
            logger.warning(
                "isolate --cg --cleanup failed (exit %s): %s",
                result.returncode,
                (result.stderr or "").strip(),
            )
            result = self._run_isolate(
                ["--cleanup"],
                timeout=_INIT_CLEANUP_TIMEOUT_SEC,
            )
            if result.returncode != 0:
                logger.warning(
                    "isolate --cleanup failed (exit %s): %s",
                    result.returncode,
                    (result.stderr or "").strip(),
                )

    def _cleanup_box(self) -> None:
        self._cleanup_isolate()
        self._init_box_path(None)
