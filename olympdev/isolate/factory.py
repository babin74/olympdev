from queue import Empty, Queue
from typing import Self

from olympdev.config import (
    DEFAULT_SANDBOX_QUOTA_BLOCKS,
    DEFAULT_SANDBOX_QUOTA_INODES,
)
from olympdev.isolate.runner import IsolateSandBox
from olympdev.isolate.types import IsolateQuota

_MAX_SANDBOX_COUNT = 999
_DEFAULT_QUOTA = IsolateQuota(
    blocks=DEFAULT_SANDBOX_QUOTA_BLOCKS,
    inodes=DEFAULT_SANDBOX_QUOTA_INODES,
)


class IsolateFactory:
    def __init__(self, sandbox_count: int) -> None:
        if not isinstance(sandbox_count, int) or isinstance(sandbox_count, bool):
            raise TypeError("sandbox_count must be an int")
        if sandbox_count < 1 or sandbox_count > _MAX_SANDBOX_COUNT:
            raise ValueError(f"sandbox_count must be in 1..{_MAX_SANDBOX_COUNT}")
        self.sandbox_count = sandbox_count
        self.queue: Queue[int] = Queue()
        for i in range(sandbox_count):
            self.queue.put(i + 1)

    def get_sandbox(
        self,
        quota: IsolateQuota | None = None,
        lock_timeout_sec: float | None = None,
    ) -> "Sandbox":
        applied = _DEFAULT_QUOTA if quota is None else quota
        return Sandbox(self, applied, lock_timeout_sec)


class Sandbox(IsolateSandBox):
    def __init__(
        self,
        factory: IsolateFactory,
        quota: IsolateQuota | None = None,
        lock_timeout_sec: float | None = None,
    ) -> None:
        self.factory = factory
        self._slot_held = False
        try:
            box_id = self.factory.queue.get(timeout=lock_timeout_sec)
        except Empty as exc:
            raise TimeoutError("timed out waiting for isolate sandbox slot") from exc
        self._slot_held = True
        self._box_id_slot = box_id
        try:
            super().__init__(
                box_id, quota=quota, lock_timeout_sec=lock_timeout_sec
            )
        except BaseException:
            self._return_slot()
            raise

    def _return_slot(self) -> None:
        if not self._slot_held:
            return
        self._slot_held = False
        self.factory.queue.put(self._box_id_slot)

    def __enter__(self) -> Self:
        try:
            super().__enter__()
        except BaseException:
            self._return_slot()
            raise
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        try:
            super().__exit__(exc_type, exc_value, traceback)
        finally:
            self._return_slot()
