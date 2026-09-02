"""Bounded, private JSON-RPC pipes to one owned Codex App Server process."""

from __future__ import annotations

import json
import os
import selectors
import subprocess
import threading
import time
import weakref
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from narumi.errors import CancelledError, EngineUnavailableError
from narumi.providers.codex import _supervisor

MAX_MESSAGE_BYTES = 8 * 1024 * 1024
MAX_SESSION_BYTES = 32 * 1024 * 1024
MAX_NOTIFICATIONS = 1000
_POLL_INTERVAL = 0.1
_rpc_instances: weakref.WeakSet[StdioRPC] = weakref.WeakSet()


def _after_fork_child() -> None:
    for rpc in tuple(_rpc_instances):
        rpc._cancelled = threading.Event()
        rpc._cancelled.set()
        rpc._closed = threading.Event()
        rpc._closed.set()
        rpc._should_cancel = lambda: False
        rpc._lifecycle_lock = threading.Lock()
        rpc._io_gate = threading.Lock()
        rpc._rpc_lock = threading.RLock()
        rpc._termination_done = True
        rpc._process = None


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork_child)


def unavailable(reason: str) -> EngineUnavailableError:
    return EngineUnavailableError(
        "Codex App Server could not complete the operation", details={"reason": reason}
    )


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate field")
        result[key] = value
    return result


def _constant(_: str) -> None:
    raise ValueError("non-finite number")


class StdioRPC:
    """Serialize RPC calls; cancellation never waits on the reader's lock.

    This is an internal transport, not a public arbitrary-command interface. The
    backend supplies only the verified fixed runtime and an allowlisted environment.
    No stderr or untrusted protocol text is copied into an exception or a log.
    """

    def __init__(
        self,
        command: Sequence[str],
        *,
        env: Mapping[str, str],
        cwd: Path,
        should_cancel: Callable[[], bool] | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.timeout = timeout
        self._should_cancel = should_cancel or (lambda: False)
        self._cancelled = threading.Event()
        self._closed = threading.Event()
        self._io_gate = threading.Lock()
        self._rpc_lock = threading.RLock()
        self._lifecycle_lock = threading.Lock()
        self._termination_done = False
        self._counter = 0
        self._input = bytearray()
        self._received = 0
        self._notifications: deque[dict[str, Any]] = deque()
        self._process: subprocess.Popen[bytes] | _supervisor.SupervisedProcess | None = None
        _rpc_instances.add(self)
        self._check_cancelled()
        try:
            self._process = (
                _supervisor.start(list(command), dict(env), cwd)
                if os.name == "posix"
                else subprocess.Popen(
                    list(command),
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    env=dict(env),
                    cwd=cwd,
                    bufsize=0,
                    start_new_session=True,
                    close_fds=True,
                    umask=0o077,
                )
            )
            if self._process.stdin is None or self._process.stdout is None:
                raise OSError("private pipes unavailable")
            os.set_blocking(self._process.stdin.fileno(), False)
            os.set_blocking(self._process.stdout.fileno(), False)
        except BaseException as error:
            for _ in range(2):
                try:
                    self.close()
                    break
                except BaseException:
                    continue
            if isinstance(error, (OSError, ValueError)):
                raise unavailable("codex_process_unavailable") from None
            raise

    def __enter__(self) -> StdioRPC:
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def _check_cancelled(self) -> None:
        if self._closed.is_set():
            raise unavailable("codex_process_closed")
        if self._cancelled.is_set() or self._should_cancel():
            raise CancelledError("Codex operation was cancelled")
        process = self._process
        if isinstance(process, _supervisor.SupervisedProcess) and (
            (watchdog_status := process.watchdog.poll()) is not None and watchdog_status < 0
        ):
            self._terminate()
            raise unavailable("codex_process_unavailable")

    def _check_io_state(self) -> None:
        if self._closed.is_set():
            raise unavailable("codex_process_closed")
        if self._cancelled.is_set():
            raise CancelledError("Codex operation was cancelled")

    def _deadline(self, timeout: float | None) -> float:
        duration = self.timeout if timeout is None else timeout
        if not 0 < duration <= 3600:
            raise unavailable("codex_invalid_timeout")
        return time.monotonic() + duration

    @contextmanager
    def _rpc_operation(self, deadline: float):
        acquired = False
        try:
            while not acquired:
                self._check_io_state()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise unavailable("codex_rpc_timeout")
                acquired = self._rpc_lock.acquire(timeout=min(remaining, _POLL_INTERVAL))
            self._check_io_state()
            yield
        finally:
            if acquired:
                self._rpc_lock.release()

    def _check_deadline(self, deadline: float) -> None:
        self._check_cancelled()
        if time.monotonic() >= deadline:
            raise unavailable("codex_rpc_timeout")

    def _wait(
        self,
        descriptor: int,
        event: int,
        deadline: float,
        lease: _supervisor.FDLease | None = None,
    ) -> None:
        with selectors.DefaultSelector() as selector:
            try:
                if lease is None:
                    selector.register(descriptor, event)
                else:
                    _supervisor._lease_register(selector, lease, event)
                while True:
                    self._check_cancelled()
                    process = self._process
                    if (
                        isinstance(process, _supervisor.SupervisedProcess)
                        and process.watchdog.poll() is not None
                    ):
                        return
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise unavailable("codex_rpc_timeout")
                    if selector.select(min(remaining, _POLL_INTERVAL)):
                        return
            except (OSError, ValueError):
                self._check_cancelled()
                raise unavailable("codex_pipe_unavailable") from None

    def _send(
        self,
        message: dict[str, Any],
        deadline: float,
        *,
        on_sent: Callable[[], None] | None = None,
    ) -> None:
        try:
            encoded = (
                json.dumps(message, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
                + "\n"
            ).encode("utf-8")
        except (TypeError, ValueError, RecursionError):
            raise unavailable("codex_invalid_request") from None
        if len(encoded) > MAX_MESSAGE_BYTES:
            raise unavailable("codex_request_limit")
        self._check_cancelled()
        process = self._process
        if process is None or process.stdin is None:
            raise unavailable("codex_process_closed")
        written = 0
        notified = False
        try:
            descriptor = process.stdin.fileno()
            lease = (
                process.stdin_lease if isinstance(process, _supervisor.SupervisedProcess) else None
            )
            while written < len(encoded):
                self._wait(descriptor, selectors.EVENT_WRITE, deadline, lease)
                if not notified and on_sent is not None:
                    self._check_cancelled()
                    on_sent()
                    notified = True
                try:
                    payload = encoded[written : written + 65536]
                    self._check_cancelled()
                    with self._io_gate:
                        self._check_io_state()
                        if lease is None:
                            amount = os.write(descriptor, payload)
                        else:
                            amount = _supervisor._lease_write(lease, payload)
                except BlockingIOError:
                    continue
                if amount <= 0:
                    raise OSError("closed pipe")
                written += amount
        except (OSError, ValueError):
            self._check_cancelled()
            raise unavailable("codex_pipe_unavailable") from None

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        deadline = self._deadline(None)
        with self._rpc_operation(deadline):
            message: dict[str, Any] = {"method": method}
            if params is not None:
                message["params"] = params
            self._send(message, deadline)

    def call(
        self,
        method: str,
        params: dict[str, Any],
        *,
        timeout: float | None = None,
        on_sent: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        deadline = self._deadline(timeout)
        with self._rpc_operation(deadline):
            self._counter += 1
            identifier = self._counter
            self._send(
                {"id": identifier, "method": method, "params": params},
                deadline,
                on_sent=on_sent,
            )
            while True:
                message = self._message(deadline)
                if "id" not in message:
                    self._queue(message)
                    continue
                if type(message["id"]) is not int or message["id"] != identifier:
                    raise unavailable("codex_response_id_mismatch")
                if "error" in message:
                    raise unavailable("codex_rpc_failed")
                result = message.get("result")
                if not isinstance(result, dict):
                    raise unavailable("codex_invalid_response")
                return result

    def wait_for(
        self,
        predicate: Callable[[dict[str, Any]], bool],
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        deadline = self._deadline(timeout)
        with self._rpc_operation(deadline):
            while True:
                self._check_deadline(deadline)
                message = (
                    self._notifications.popleft()
                    if self._notifications
                    else self._message(deadline)
                )
                if "id" in message:
                    raise unavailable("codex_unexpected_response")
                if predicate(message):
                    return message

    def _queue(self, message: dict[str, Any]) -> None:
        if len(self._notifications) >= MAX_NOTIFICATIONS:
            raise unavailable("codex_notification_limit")
        self._notifications.append(message)

    def _message(self, deadline: float) -> dict[str, Any]:
        while True:
            self._check_deadline(deadline)
            newline = self._input.find(b"\n")
            if newline >= 0:
                if newline > MAX_MESSAGE_BYTES:
                    raise unavailable("codex_response_limit")
                line = bytes(self._input[:newline])
                del self._input[: newline + 1]
                if not line.strip():
                    continue
                try:
                    value = json.loads(line, object_pairs_hook=_object, parse_constant=_constant)
                except (ValueError, UnicodeError, RecursionError):
                    raise unavailable("codex_invalid_json") from None
                return self._validate(value, deadline)
            if len(self._input) > MAX_MESSAGE_BYTES:
                raise unavailable("codex_response_limit")
            process = self._process
            if process is None or process.stdout is None:
                raise unavailable("codex_process_closed")
            try:
                descriptor = process.stdout.fileno()
                lease = (
                    process.stdout_lease
                    if isinstance(process, _supervisor.SupervisedProcess)
                    else None
                )
                self._wait(descriptor, selectors.EVENT_READ, deadline, lease)
                try:
                    self._check_cancelled()
                    with self._io_gate:
                        self._check_io_state()
                        if lease is None:
                            chunk = os.read(descriptor, 65536)
                        else:
                            chunk = _supervisor._lease_read(lease, 65536)
                except BlockingIOError:
                    continue
            except (OSError, ValueError):
                self._check_cancelled()
                raise unavailable("codex_pipe_unavailable") from None
            self._check_cancelled()
            if not chunk:
                raise unavailable("codex_process_eof")
            self._received += len(chunk)
            if self._received > MAX_SESSION_BYTES:
                raise unavailable("codex_session_limit")
            self._input.extend(chunk)

    def _validate(self, value: Any, deadline: float) -> dict[str, Any]:
        if not isinstance(value, dict) or value.get("jsonrpc", "2.0") != "2.0":
            raise unavailable("codex_invalid_response")
        if "method" in value:
            method = value["method"]
            if not isinstance(method, str) or not method or len(method) > 256:
                raise unavailable("codex_invalid_notification")
            if not isinstance(value.get("params", {}), dict):
                raise unavailable("codex_invalid_notification")
            if "id" in value:
                identifier = value["id"]
                if type(identifier) is int or (
                    isinstance(identifier, str) and len(identifier) <= 256
                ):
                    self._send(
                        {
                            "id": identifier,
                            "error": {"code": -32601, "message": "Client tools are disabled"},
                        },
                        deadline,
                    )
                raise unavailable("codex_server_request_rejected")
            if "result" in value or "error" in value:
                raise unavailable("codex_invalid_notification")
            if method in {"model/rerouted", "modelRerouted", "configWarning", "warning"}:
                # Reject an observed policy change before a later prompt can be sent.
                raise unavailable("codex_runtime_configuration_changed")
        elif "id" not in value or (("result" in value) == ("error" in value)):
            raise unavailable("codex_invalid_response")
        return value

    def cancel(self) -> None:
        with self._io_gate:
            self._cancelled.set()
        self._terminate()

    @contextmanager
    def cooperative_cleanup(self):
        """Permit a bounded interrupt RPC after cooperative cancellation only.

        An explicit cancel()/close() still stops all I/O. This never starts another
        model turn, and the backend does not share a transport across operations.
        """
        deadline = self._deadline(None)
        with self._rpc_operation(deadline):
            original = self._should_cancel
            self._should_cancel = lambda: False
            try:
                yield
            finally:
                self._should_cancel = original

    def _terminate(self) -> None:
        with self._lifecycle_lock:
            process = self._process
            if process is None or self._termination_done:
                return
            if isinstance(process, _supervisor.SupervisedProcess):
                if _supervisor.terminate(process):
                    self._termination_done = True
                return
            try:
                process.terminate()
                process.wait(timeout=0.1)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    process.kill()
                    process.wait(timeout=2)
                except (ProcessLookupError, PermissionError, subprocess.TimeoutExpired):
                    pass
            self._termination_done = process.poll() is not None

    def close(self) -> None:
        with self._io_gate:
            self._closed.set()
        self._terminate()
        process = self._process
        if process is not None:
            for stream in (process.stdin, process.stdout):
                if stream is not None:
                    stream.close()
