"""``request_id`` replay: a write tool called twice with the same key returns the first result.

Successful responses are stored in the catalog's ``requests`` table; a replay returns the stored
response unchanged and runs nothing. Errors are not stored, so a failed call may be retried with
the same key. Concurrent calls sharing one key are serialized so at most one of them executes,
except recording-permission actions, whose in-flight replays immediately return busy.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Mapping
from typing import Any

from narumi.catalog import Catalog
from narumi.contracts import ToolContract
from narumi.errors import BusyError, InvalidArgumentError

logger = logging.getLogger(__name__)

REQUEST_ID_KEY = "request_id"
NONBLOCKING_TOOLS = frozenset({"configure_recording_permission"})


class IdempotencyStore:
    def __init__(self, catalog: Catalog) -> None:
        self._catalog = catalog
        self._guard = threading.Lock()
        self._inflight: dict[str, threading.Lock] = {}

    def run(
        self,
        contract: ToolContract,
        args: Mapping[str, Any],
        fn: Callable[[], Mapping[str, Any]],
    ) -> dict[str, Any]:
        """Execute ``fn`` unless ``args["request_id"]`` was already answered for this tool."""
        request_id = None if contract.read_only else args.get(REQUEST_ID_KEY)
        if not isinstance(request_id, str) or not request_id:
            return dict(fn())
        nonblocking = contract.name in NONBLOCKING_TOOLS
        lock = self._lock_for(request_id, acquire_nonblocking=nonblocking)
        if lock is None:
            raise BusyError("this recording permission request is already in progress")
        if not nonblocking:
            lock.acquire()
        try:
            cached = self._catalog.get_request(request_id)
            if cached is not None:
                if cached["tool"] != contract.name:
                    raise InvalidArgumentError(
                        f"request_id {request_id!r} was already used for tool {cached['tool']!r}",
                        details={"request_id": request_id, "tool": cached["tool"]},
                    )
                if contract.name in NONBLOCKING_TOOLS and any(
                    cached["response"].get(key) != args.get(key) for key in ("permission", "action")
                ):
                    raise InvalidArgumentError(
                        "request_id was already used with different permission arguments"
                    )
                logger.info("replaying %s for request_id %s", contract.name, request_id)
                return dict(cached["response"])
            result = dict(fn())
            meeting_id = result.get("meeting_id")
            if not isinstance(meeting_id, str):
                candidate = args.get("meeting_id")
                meeting_id = candidate if isinstance(candidate, str) else None
            self._catalog.save_request(request_id, contract.name, meeting_id, result)
            return result
        finally:
            lock.release()

    def _lock_for(
        self, request_id: str, *, acquire_nonblocking: bool = False
    ) -> threading.Lock | None:
        with self._guard:
            lock = self._inflight.get(request_id)
            if lock is None:
                lock = threading.Lock()
                self._inflight[request_id] = lock
                if len(self._inflight) > 4096:  # bounded: keys are one-shot
                    for key in list(self._inflight)[:2048]:
                        if not self._inflight[key].locked():
                            del self._inflight[key]
                # The bounded cleanup may have selected the newly inserted, unlocked key.
                self._inflight[request_id] = lock
            # Acquire new permission keys under the registry guard so cleanup cannot evict
            # their lock between lookup and acquisition and permit a duplicate operation.
            if acquire_nonblocking and not lock.acquire(blocking=False):
                return None
            return lock
