"""``request_id`` replay: a write tool called twice with the same key returns the first result.

Successful responses are stored in the catalog's ``requests`` table; a replay returns the stored
response unchanged and runs nothing. Errors are not stored, so a failed call may be retried with
the same key. Concurrent calls sharing one key are serialized so at most one of them executes.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Mapping
from typing import Any

from narumi.catalog import Catalog
from narumi.contracts import ToolContract
from narumi.errors import InvalidArgumentError

logger = logging.getLogger(__name__)

REQUEST_ID_KEY = "request_id"


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
        lock = self._lock_for(request_id)
        with lock:
            cached = self._catalog.get_request(request_id)
            if cached is not None:
                if cached["tool"] != contract.name:
                    raise InvalidArgumentError(
                        f"request_id {request_id!r} was already used for tool {cached['tool']!r}",
                        details={"request_id": request_id, "tool": cached["tool"]},
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

    def _lock_for(self, request_id: str) -> threading.Lock:
        with self._guard:
            lock = self._inflight.get(request_id)
            if lock is None:
                lock = threading.Lock()
                self._inflight[request_id] = lock
                if len(self._inflight) > 4096:  # bounded: keys are one-shot
                    for key in list(self._inflight)[:2048]:
                        if not self._inflight[key].locked():
                            del self._inflight[key]
            return lock
