"""Virtual clocks, timers and sockets exercise urllib's real HTTP header parser."""

from __future__ import annotations

import io
import socket

import pytest
from narumi.errors import EngineUnavailableError
from narumi.providers.metadata import deadline as deadline_module
from narumi.providers.metadata.deadline import RequestDeadline
from narumi.providers.metadata.http import JSONHTTPClient

LOCAL_URL = "http://127.0.0.1:11434/api/tags"


class Clock:
    def __init__(self):
        self.value = 0.0
        self.timers = []

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds
        for timer in self.timers:
            if not timer.cancelled and not timer.fired and self.value >= timer.expires:
                timer.fired = True
                timer.callback()

    def timer(self, delay, callback):
        return Timer(self, delay, callback)


class Timer:
    def __init__(self, clock, delay, callback):
        self.clock, self.callback = clock, callback
        self.expires = clock.value + delay
        self.cancelled = self.fired = False

    def start(self):
        self.clock.timers.append(self)

    def cancel(self):
        self.cancelled = True


class SocketBytes(io.RawIOBase):
    def __init__(self, connection):
        self.connection = connection

    def readable(self):
        return True

    def readinto(self, target):
        connection = self.connection
        if connection.aborted:
            raise TimeoutError("fixture socket interrupted")
        if connection.position >= len(connection.wire):
            return 0
        in_headers = connection.position < connection.header_end
        slow = (connection.phase == "headers" and in_headers) or (
            connection.phase == "body" and not in_headers
        )
        if slow:
            # Each byte arrives well within a socket inactivity timeout. Only the
            # independently scheduled absolute deadline can stop header parsing.
            connection.clock.advance(0.2)
            if connection.aborted:
                raise TimeoutError("fixture socket interrupted")
            size = 1
        else:
            end = connection.header_end if in_headers else len(connection.wire)
            size = min(len(target), end - connection.position)
        target[:size] = connection.wire[connection.position : connection.position + size]
        connection.position += size
        return size


class Socket:
    def __init__(self, clock, *, phase=None):
        body = b'{"models":[],"unused":"fixture body padding for deadline testing"}'
        headers = (
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\n"
            + f"Content-Length: {len(body)}\r\n\r\n".encode()
        )
        self.wire = headers + body
        self.header_end = len(headers)
        self.clock, self.phase = clock, phase
        self.aborted = False
        self.closed = False
        self.position = 0
        self.sent = []
        self.timeouts = []

    def settimeout(self, timeout):
        self.timeouts.append(timeout)

    def connect(self, address):
        assert address == ("127.0.0.1", 11434), "unexpected destination"

    def sendall(self, data):
        self.sent.append(data)

    def makefile(self, mode):
        assert mode == "rb"
        return io.BufferedReader(SocketBytes(self))

    def shutdown(self, how):
        assert how == socket.SHUT_RDWR
        self.aborted = True

    def close(self):
        # HTTPResponse holds the file reference after HTTPConnection.close().
        self.closed = True


def install_clock_and_socket(monkeypatch, *, phase=None):
    clock = Clock()
    connection = Socket(clock, phase=phase)
    monkeypatch.setattr(deadline_module.threading, "Timer", clock.timer)
    monkeypatch.setattr(deadline_module.socket, "socket", lambda *args: connection)

    def no_dns(*args, **kwargs):
        raise AssertionError("numeric loopback must not consult DNS")

    monkeypatch.setattr(deadline_module.socket, "getaddrinfo", no_dns)
    return clock, connection


@pytest.mark.parametrize("phase", ["headers", "body"])
def test_absolute_deadline_interrupts_slow_trickle_in_headers_and_body(monkeypatch, phase):
    clock, connection = install_clock_and_socket(monkeypatch, phase=phase)
    with pytest.raises(EngineUnavailableError) as failure:
        JSONHTTPClient(monotonic=clock).request("GET", LOCAL_URL, timeout=3)
    assert failure.value.details["reason"] == "metadata_connection_failed"
    assert clock.value <= 3.21
    assert connection.aborted and connection.closed
    assert clock.timers[0].fired
    if phase == "headers":
        assert connection.position < connection.header_end
    else:
        assert connection.position >= connection.header_end


def test_success_cancels_deadline_and_closes_the_transport(monkeypatch):
    clock, connection = install_clock_and_socket(monkeypatch)
    result = JSONHTTPClient(monotonic=clock).request("GET", LOCAL_URL, timeout=3)
    assert result["models"] == []
    assert connection.closed
    assert clock.timers[0].cancelled and not clock.timers[0].fired
    assert connection.timeouts == [3]


def test_deadline_expiring_during_tls_handshake_aborts_tls_socket(monkeypatch):
    clock, connection = install_clock_and_socket(monkeypatch)
    guard = RequestDeadline(3, monotonic=clock)
    guard.start()

    class TLSContext:
        def wrap_socket(self, raw, *, server_hostname, do_handshake_on_connect):
            assert raw is connection and server_hostname == "127.0.0.1"
            assert do_handshake_on_connect is False
            return connection

    def handshake():
        clock.advance(3)
        if connection.aborted:
            raise TimeoutError("fixture TLS interrupted")

    connection.do_handshake = handshake
    request = deadline_module._HTTPSConnection(
        "127.0.0.1", 11434, context=TLSContext(), deadline=guard
    )
    try:
        with pytest.raises(TimeoutError):
            request.connect()
        assert connection.aborted
    finally:
        guard.close()


def test_dns_wait_has_a_deadline_without_passing_request_data_to_resolver(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(deadline_module.threading, "Timer", clock.timer)
    guard = RequestDeadline(3, monotonic=clock)
    guard.start()
    targets = []

    class PendingThread:
        def __init__(self, *, target, daemon):
            assert daemon is True
            targets.append(target)

        def start(self):
            pass

    class PendingEvent:
        def wait(self, timeout):
            clock.advance(timeout)
            return False

        def set(self):
            pass

    monkeypatch.setattr(deadline_module.threading, "Thread", PendingThread)
    monkeypatch.setattr(deadline_module.threading, "Event", PendingEvent)
    # The simulated stuck resolver retains one slot until it eventually exits.
    # Use an isolated semaphore so this test does not retain a production slot.
    monkeypatch.setattr(deadline_module, "_DNS_SLOTS", __import__("threading").BoundedSemaphore(4))
    try:
        with pytest.raises(TimeoutError):
            deadline_module._resolve("api.anthropic.com", 443, guard)
        assert clock.value == 3
        assert len(targets) == 1
        assert set(targets[0].__code__.co_freevars) == {"finished", "host", "port", "result"}
    finally:
        guard.close()
