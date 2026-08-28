"""An OS lease proves that unresolved preparation is no longer executing locally."""

from __future__ import annotations

import fcntl
import os
import threading
from pathlib import Path

from narumi.errors import BusyError
from narumi.providers._io import _open_directory, _open_regular


class RuntimeLease:
    def __init__(self, root: Path, provider_id: str) -> None:
        directory = _open_directory(
            root / "providers" / "runtime" / provider_id,
            trusted_root=root,
        )
        try:
            descriptor = _open_regular(directory, ".prepare.lock", os.O_CREAT | os.O_RDWR)
        finally:
            os.close(directory)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(descriptor)
            raise BusyError("Provider runtime preparation is still executing") from None
        except BaseException:
            os.close(descriptor)
            raise
        self._descriptor: int | None = descriptor
        self._lock = threading.Lock()

    def release(self) -> None:
        with self._lock:
            if self._descriptor is not None:
                os.close(self._descriptor)
                self._descriptor = None
