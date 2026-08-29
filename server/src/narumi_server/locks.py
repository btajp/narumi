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
from contextlib import ExitStack, contextmanager
from pathlib import Path

from narumi.bundle import manifest_writer_lock
from narumi.errors import BusyError

HANDLER_WAIT_SECONDS = 5.0
"""How long a tool handler waits for the lock before answering ``busy``."""


class MeetingLocks:
    def __init__(self, meetings_root: Path) -> None:
        self._meetings_root = Path(meetings_root)
        self._guard = threading.Lock()
        self._holders: dict[str, list[str]] = {}

    def holder(self, meeting_id: str) -> str | None:
        """Purpose of the current holder (``None`` when the meeting is not locked)."""
        with self._guard:
            purposes = self._holders.get(meeting_id)
            return purposes[-1] if purposes else None

    @contextmanager
    def hold(
        self, meeting_id: str, *, purpose: str, timeout: float | None = None
    ) -> Iterator[None]:
        """Hold the meeting's lock; ``busy`` when it is not free within ``timeout`` seconds.

        ``timeout=None`` waits indefinitely (for jobs, which must never fail on a short handler
        write). The manifest fence applies one deadline to its process-local mutex and flock.
        :meth:`Bundle.save` safely re-enters that fence on the same thread.
        """
        with ExitStack() as stack:
            try:
                stack.enter_context(
                    manifest_writer_lock(self._meetings_root, meeting_id, timeout=timeout)
                )
            except BusyError:
                raise self._busy(meeting_id) from None
            with self._guard:
                self._holders.setdefault(meeting_id, []).append(purpose)
            try:
                yield
            finally:
                with self._guard:
                    purposes = self._holders[meeting_id]
                    purposes.pop()
                    if not purposes:
                        self._holders.pop(meeting_id)

    def _busy(self, meeting_id: str) -> BusyError:
        holder = self.holder(meeting_id)
        return BusyError(
            f"meeting {meeting_id} is being modified ({holder or 'unknown'});"
            " retry when the job has finished",
            details={"meeting_id": meeting_id, "holder": holder},
        )
