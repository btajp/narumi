"""Absolute deadlines covering DNS, connection, TLS, HTTP headers and body reads."""

from __future__ import annotations

import http.client
import ipaddress
import select
import socket
import ssl
import threading
import time
import urllib.request
from collections.abc import Callable

_DNS_SLOTS = threading.BoundedSemaphore(4)
CANCEL_POLL_INTERVAL = 0.1


class RequestCancelled(Exception):
    """Internal cancellation signal; the HTTP boundary assigns public details."""


class RequestDeadline:
    """Abort the actual socket even while urllib is blocked reading HTTP headers."""

    def __init__(
        self,
        timeout: float,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        should_cancel: Callable[[], bool] | None = None,
        interruptible_write: bool = False,
    ):
        self._clock = monotonic
        self._expires_at = monotonic() + timeout
        self._expired = threading.Event()
        self._cancelled = threading.Event()
        self._closed = threading.Event()
        self._should_cancel = should_cancel
        self._lock = threading.Lock()
        self._socket: socket.socket | None = None
        self._cancel_timer: threading.Timer | None = None
        self._timer = threading.Timer(timeout, self.expire)
        self._timer.daemon = True
        self.request_started = False
        self.interruptible_write = interruptible_write

    def start(self) -> None:
        self.remaining()
        self._timer.start()
        if self._should_cancel is not None:
            self._poll_cancel()

    @property
    def cancelled(self) -> bool:
        if not self._cancelled.is_set() and self._should_cancel is not None:
            try:
                cancelled = self._should_cancel()
            except Exception:
                # A failing cancellation callback must not leak on a timer thread
                # or permit an operation to run without cancellation protection.
                cancelled = True
            if cancelled:
                self._cancelled.set()
                self._abort_socket()
        return self._cancelled.is_set()

    def remaining(self) -> float:
        if self.cancelled:
            raise RequestCancelled("HTTP request cancelled")
        remaining = self._expires_at - self._clock()
        if self._expired.is_set() or remaining <= 0:
            self.expire()
            raise TimeoutError("HTTP request deadline exceeded")
        return remaining

    def wait_timeout(self) -> float:
        remaining = self.remaining()
        return min(remaining, CANCEL_POLL_INTERVAL) if self._should_cancel else remaining

    def mark_request_started(self) -> None:
        """Called after connect/TLS, immediately before the first HTTP write."""
        self.remaining()
        self.request_started = True

    def _poll_cancel(self) -> None:
        if self._closed.is_set() or self.cancelled or self._expired.is_set():
            return
        with self._lock:
            if not self._closed.is_set():
                self._cancel_timer = threading.Timer(CANCEL_POLL_INTERVAL, self._poll_cancel)
                self._cancel_timer.daemon = True
                self._cancel_timer.start()

    def track(self, connection: socket.socket) -> None:
        with self._lock:
            self._socket = connection
        connection.settimeout(self.remaining())

    def expire(self) -> None:
        self._expired.set()
        self._abort_socket()

    def _abort_socket(self) -> None:
        with self._lock:
            connection = self._socket
        if connection is not None:
            _abort(connection)

    def close(self) -> None:
        self._closed.set()
        self._timer.cancel()
        with self._lock:
            if self._cancel_timer is not None:
                self._cancel_timer.cancel()
            connection, self._socket = self._socket, None
        if connection is not None:
            _abort(connection)


def _abort(connection: socket.socket) -> None:
    try:
        connection.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        connection.close()
    except OSError:
        pass


def _resolve(host: str, port: int, deadline: RequestDeadline) -> list[tuple]:
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        family = socket.AF_INET if address.version == 4 else socket.AF_INET6
        target = (host, port) if family == socket.AF_INET else (host, port, 0, 0)
        return [(family, socket.SOCK_STREAM, 0, "", target)]
    # System DNS calls cannot be cancelled portably. Bound their number and stop
    # waiting at the deadline; the detached resolver only holds a host and port.
    while not _DNS_SLOTS.acquire(timeout=deadline.wait_timeout()):
        deadline.remaining()
    try:
        deadline.remaining()
    except Exception:
        _DNS_SLOTS.release()
        raise
    finished = threading.Event()
    result: list[list[tuple] | None] = []

    def resolve() -> None:
        try:
            result.append(socket.getaddrinfo(host, port, type=socket.SOCK_STREAM))
        except Exception:
            result.append(None)
        finally:
            finished.set()
            _DNS_SLOTS.release()

    try:
        threading.Thread(target=resolve, daemon=True).start()
    except Exception:
        _DNS_SLOTS.release()
        raise OSError("DNS resolver unavailable") from None
    while not finished.wait(deadline.wait_timeout()):
        deadline.remaining()
    deadline.remaining()
    if not result or not result[0]:
        raise OSError("DNS resolution failed")
    return result[0][:16]


def _connect(host: str, port: int, deadline: RequestDeadline) -> socket.socket:
    for family, kind, protocol, _, address in _resolve(host, port, deadline):
        deadline.remaining()
        connection = socket.socket(family, kind, protocol)
        try:
            deadline.track(connection)
            connection.connect(address)
            deadline.remaining()
            return connection
        except OSError:
            connection.close()
    raise OSError("HTTP connection failed")


class _TrackedSend:
    def send(self, data) -> None:
        # HTTPConnection.send() normally connects lazily. Complete that step
        # first so a DNS/connect/TLS failure is never counted as a transmitted
        # generation request. Once sendall may run, the outcome is uncertain.
        if self.sock is None:
            if not self.auto_open:
                raise http.client.NotConnected()
            self.connect()
        self.deadline.mark_request_started()
        if self.deadline.interruptible_write and isinstance(data, bytes):
            self._send_interruptibly(data)
            return
        super().send(data)

    def _send_interruptibly(self, data: bytes) -> None:
        # A blocking sendall can outlive shutdown from another thread while a
        # large upload is flow-controlled. Continue only the application bytes
        # not yet accepted by send(); never restart the request or its body.
        connection = self.sock
        original_timeout = connection.gettimeout()
        pending = memoryview(data)
        connection.setblocking(False)
        try:
            while pending:
                self.deadline.remaining()
                try:
                    sent = connection.send(pending)
                except ssl.SSLWantReadError:
                    readers, writers = [connection], []
                except (BlockingIOError, ssl.SSLWantWriteError):
                    readers, writers = [], [connection]
                else:
                    if sent <= 0:
                        raise OSError("HTTP upload connection closed")
                    pending = pending[sent:]
                    continue
                select.select(
                    readers, writers, [], min(CANCEL_POLL_INTERVAL, self.deadline.remaining())
                )
        finally:
            try:
                connection.settimeout(original_timeout)
            except OSError:
                pass


class _HTTPConnection(_TrackedSend, http.client.HTTPConnection):
    def __init__(self, *args, deadline: RequestDeadline, **kwargs):
        super().__init__(*args, **kwargs)
        self.deadline = deadline

    def connect(self) -> None:
        if self._tunnel_host is not None:
            raise OSError("HTTP tunnels are not supported")
        self.sock = _connect(self.host, self.port, self.deadline)


class _HTTPSConnection(_TrackedSend, http.client.HTTPSConnection):
    def __init__(self, *args, deadline: RequestDeadline, **kwargs):
        super().__init__(*args, **kwargs)
        self.deadline = deadline

    def connect(self) -> None:
        if self._tunnel_host is not None:
            raise OSError("HTTP tunnels are not supported")
        raw = _connect(self.host, self.port, self.deadline)
        self.sock = self._context.wrap_socket(
            raw, server_hostname=self.host, do_handshake_on_connect=False
        )
        self.deadline.track(self.sock)
        self.sock.do_handshake()
        self.deadline.remaining()


class DeadlineHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, request):
        def connection(*args, **kwargs):
            return _HTTPConnection(*args, deadline=request.narumi_deadline, **kwargs)

        return self.do_open(connection, request)


class DeadlineHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, request):
        def connection(*args, **kwargs):
            return _HTTPSConnection(*args, deadline=request.narumi_deadline, **kwargs)

        return self.do_open(connection, request, context=self._context)
