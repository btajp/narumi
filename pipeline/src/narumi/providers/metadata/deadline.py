"""Absolute deadlines covering DNS, connection, TLS, HTTP headers and body reads."""

from __future__ import annotations

import http.client
import ipaddress
import socket
import threading
import time
import urllib.request
from collections.abc import Callable

_DNS_SLOTS = threading.BoundedSemaphore(4)


class RequestDeadline:
    """Abort the actual socket even while urllib is blocked reading HTTP headers."""

    def __init__(self, timeout: float, *, monotonic: Callable[[], float] = time.monotonic):
        self._clock = monotonic
        self._expires_at = monotonic() + timeout
        self._expired = threading.Event()
        self._lock = threading.Lock()
        self._socket: socket.socket | None = None
        self._timer = threading.Timer(timeout, self.expire)
        self._timer.daemon = True

    def start(self) -> None:
        self._timer.start()

    def remaining(self) -> float:
        remaining = self._expires_at - self._clock()
        if self._expired.is_set() or remaining <= 0:
            self.expire()
            raise TimeoutError("HTTP request deadline exceeded")
        return remaining

    def track(self, connection: socket.socket) -> None:
        with self._lock:
            self._socket = connection
        connection.settimeout(self.remaining())

    def expire(self) -> None:
        self._expired.set()
        with self._lock:
            connection = self._socket
        if connection is not None:
            _abort(connection)

    def close(self) -> None:
        self._timer.cancel()
        with self._lock:
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
    if not _DNS_SLOTS.acquire(timeout=deadline.remaining()):
        raise TimeoutError("DNS deadline exceeded")
    finished = threading.Event()
    result: list[list[tuple] | None] = []

    def resolve() -> None:
        try:
            result.append(socket.getaddrinfo(host, port, type=socket.SOCK_STREAM))
        except OSError:
            result.append(None)
        finally:
            finished.set()
            _DNS_SLOTS.release()

    try:
        threading.Thread(target=resolve, daemon=True).start()
    except RuntimeError:
        _DNS_SLOTS.release()
        raise OSError("DNS resolver unavailable") from None
    if not finished.wait(deadline.remaining()):
        raise TimeoutError("DNS deadline exceeded")
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


class _HTTPConnection(http.client.HTTPConnection):
    def __init__(self, *args, deadline: RequestDeadline, **kwargs):
        super().__init__(*args, **kwargs)
        self.deadline = deadline

    def connect(self) -> None:
        if self._tunnel_host is not None:
            raise OSError("HTTP tunnels are not supported")
        self.sock = _connect(self.host, self.port, self.deadline)


class _HTTPSConnection(http.client.HTTPSConnection):
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
