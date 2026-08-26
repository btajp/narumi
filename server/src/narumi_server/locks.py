"""Per-meeting mutual exclusion for everything that writes ``manifest.json``.

``Bundle.save()`` rewrites the whole manifest from memory, so two writers holding their own
``Bundle`` instance silently revert each other's changes (a running pipeline job vs. a tool call
such as ``register_context`` / ``set_meeting_config`` / ``export_minutes`` / ``stop_recording``).
Every manifest writer in the server therefore takes the meeting's lock for its whole
read → modify → save sequence: jobs hold it for their entire run (blocking), tool handlers wait a
short moment and answer ``busy`` when a job owns it.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager

from narumi.errors import BusyError

HANDLER_WAIT_SECONDS = 5.0
"""How long a tool handler waits for the lock before answering ``busy``."""


class MeetingLocks:
    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}
        self._holders: dict[str, str] = {}

    def _lock_for(self, meeting_id: str) -> threading.Lock:
        with self._guard:
            lock = self._locks.get(meeting_id)
            if lock is None:
                lock = threading.Lock()
                self._locks[meeting_id] = lock
            return lock

    def holder(self, meeting_id: str) -> str | None:
        """Purpose of the current holder (``None`` when the meeting is not locked)."""
        with self._guard:
            return self._holders.get(meeting_id)

    @contextmanager
    def hold(
        self, meeting_id: str, *, purpose: str, timeout: float | None = None
    ) -> Iterator[None]:
        """Hold the meeting's lock; ``busy`` when it is not free within ``timeout`` seconds.

        ``timeout=None`` waits indefinitely (for jobs, which must never fail on a short handler
        write). The lock is not reentrant: a holder must not call code that takes it again.
        """
        lock = self._lock_for(meeting_id)
        acquired = lock.acquire(timeout=-1 if timeout is None else max(0.0, timeout))
        if not acquired:
            raise BusyError(
                f"meeting {meeting_id} is being modified ({self.holder(meeting_id) or 'unknown'});"
                " retry when the job has finished",
                details={"meeting_id": meeting_id, "holder": self.holder(meeting_id)},
            )
        with self._guard:
            self._holders[meeting_id] = purpose
        try:
            yield
        finally:
            with self._guard:
                self._holders.pop(meeting_id, None)
            lock.release()
