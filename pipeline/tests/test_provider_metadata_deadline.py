"""Virtual clocks, timers and sockets exercise urllib's real HTTP header parser."""

from __future__ import annotations

import io
import socket
import ssl
import threading
import time
from contextlib import contextmanager

import pytest
from narumi.errors import CancelledError, EngineUnavailableError
from narumi.providers.metadata import deadline as deadline_module
from narumi.providers.metadata.deadline import RequestCancelled, RequestDeadline
from narumi.providers.metadata.http import JSONHTTPClient, RejectRedirects

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
        self.blocking = []
        self.current_timeout = None

    def settimeout(self, timeout):
        self.timeouts.append(timeout)
        self.current_timeout = timeout

    def setblocking(self, value):
        self.blocking.append(value)
        self.current_timeout = None if value else 0.0

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


@pytest.mark.parametrize("phase", ["connect", "headers", "body"])
def test_cancel_aborts_each_http_phase_and_tracks_only_actual_http_send(monkeypatch, phase):
    clock, connection = install_clock_and_socket(monkeypatch, phase=phase)
    if phase == "connect":

        def connect(address):
            clock.advance(0.4)
            if connection.aborted:
                raise OSError("fixture interrupted connect")

        connection.connect = connect
    with pytest.raises(CancelledError) as failure:
        JSONHTTPClient(monotonic=clock).request(
            "POST",
            LOCAL_URL,
            payload={"prompt": "fixture"},
            timeout=30,
            response_kind="generation",
            should_cancel=lambda: clock.value >= 0.3,
        )
    assert clock.value <= 0.41
    assert connection.aborted and connection.closed
    assert not connection.sent if phase == "connect" else connection.sent
    assert failure.value.details == (
        {"reason": "provider_generation_cancelled"}
        if phase == "connect"
        else {"reason": "provider_generation_outcome_unknown", "outcome_unknown": True}
    )
    assert all(timer.cancelled or timer.fired for timer in clock.timers)


def test_cancel_during_tls_handshake_closes_socket_without_marking_http_send(monkeypatch):
    clock, connection = install_clock_and_socket(monkeypatch)
    guard = RequestDeadline(30, monotonic=clock, should_cancel=lambda: clock.value >= 0.3)
    guard.start()

    class TLSContext:
        def wrap_socket(self, raw, **kwargs):
            assert raw is connection
            return connection

    def handshake():
        clock.advance(0.4)
        if connection.aborted:
            raise OSError("fixture interrupted handshake")

    connection.do_handshake = handshake
    request = deadline_module._HTTPSConnection(
        "127.0.0.1", 11434, context=TLSContext(), deadline=guard
    )
    try:
        with pytest.raises(OSError):
            request.send(b"POST / HTTP/1.1\r\n\r\n")
        assert guard.cancelled and not guard.request_started
        assert connection.aborted and connection.closed and not connection.sent
    finally:
        guard.close()


@pytest.mark.parametrize("cancel", [False, True])
@pytest.mark.parametrize("want", ["read", "write"])
def test_tls_handshake_poll_enforces_cancellation_and_absolute_deadline(monkeypatch, cancel, want):
    clock, connection = install_clock_and_socket(monkeypatch)
    monkeypatch.setattr(deadline_module, "_abort", lambda _connection: None)
    guard = RequestDeadline(
        30 if cancel else 0.2,
        monotonic=clock,
        should_cancel=(lambda: clock.value >= 0.2) if cancel else None,
    )

    class TLSContext:
        def wrap_socket(self, raw, **kwargs):
            assert raw is connection
            return connection

    def handshake():
        raise ssl.SSLWantReadError() if want == "read" else ssl.SSLWantWriteError()

    def not_ready(readers, writers, errors, timeout):
        assert (readers, writers) == (([connection], []) if want == "read" else ([], [connection]))
        assert not errors
        clock.advance(timeout)
        return [], [], []

    connection.do_handshake = handshake
    monkeypatch.setattr(deadline_module.select, "select", not_ready)
    request = deadline_module._HTTPSConnection(
        "127.0.0.1", 11434, context=TLSContext(), deadline=guard
    )
    guard.start()
    try:
        with pytest.raises(RequestCancelled if cancel else TimeoutError):
            request.connect()
        assert clock.value <= 0.21
        assert connection.blocking == [False]
        assert not guard.request_started and not connection.sent
    finally:
        guard.close()


@pytest.mark.parametrize(
    ("mode", "expected"),
    [("cancel", RequestCancelled), ("deadline", TimeoutError), ("transport", OSError)],
)
def test_tls_select_close_race_preserves_cancel_and_deadline_precedence(
    monkeypatch, mode, expected
):
    clock, connection = install_clock_and_socket(monkeypatch)
    monkeypatch.setattr(deadline_module, "_abort", lambda _connection: None)
    guard = RequestDeadline(
        0.2 if mode == "deadline" else 30,
        monotonic=clock,
        should_cancel=(lambda: clock.value >= 0.2) if mode == "cancel" else None,
    )

    class TLSContext:
        def wrap_socket(self, raw, **kwargs):
            return raw

    def handshake():
        raise ssl.SSLWantReadError()

    connection.do_handshake = handshake

    def descriptor_closed(readers, writers, errors, timeout):
        assert readers == [connection] and not writers and not errors
        if mode != "transport":
            clock.advance(0.2)
        raise ValueError("file descriptor cannot be a negative integer")

    monkeypatch.setattr(deadline_module.select, "select", descriptor_closed)
    request = deadline_module._HTTPSConnection(
        "127.0.0.1", 11434, context=TLSContext(), deadline=guard
    )
    guard.start()
    try:
        with pytest.raises(expected):
            request.connect()
        assert not guard.request_started and not connection.sent
    finally:
        guard.close()


def test_tls_handshake_completes_before_first_http_write_is_marked(monkeypatch):
    clock, connection = install_clock_and_socket(monkeypatch)
    guard = RequestDeadline(3, monotonic=clock)
    events = []
    waits = []
    attempts = 0

    class TLSContext:
        def wrap_socket(self, raw, **kwargs):
            return raw

    def handshake():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ssl.SSLWantReadError()
        if attempts == 2:
            raise ssl.SSLWantWriteError()
        clock.advance(0.5)
        events.append(("handshake", guard.request_started))

    def ready(readers, writers, errors, timeout):
        waits.append((readers, writers, errors, timeout))
        clock.advance(0.25)
        return readers, writers, []

    def sendall(data):
        events.append(("write", guard.request_started))

    connection.do_handshake = handshake
    connection.sendall = sendall
    monkeypatch.setattr(deadline_module.select, "select", ready)
    request = deadline_module._HTTPSConnection(
        "127.0.0.1", 11434, context=TLSContext(), deadline=guard
    )
    guard.start()
    try:
        request.send(b"POST / HTTP/1.1\r\n\r\n")
        assert events == [("handshake", False), ("write", True)]
        assert [(readers, writers) for readers, writers, _, _ in waits] == [
            ([connection], []),
            ([], [connection]),
        ]
        assert [timeout for _, _, _, timeout in waits] == [3, 2.75]
        assert connection.blocking == [False]
        assert connection.timeouts == [3, 3, 2]
        assert connection.current_timeout == 2
    finally:
        guard.close()


def test_cancellation_while_waiting_for_a_dns_slot_never_starts_a_resolver(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(deadline_module.threading, "Timer", clock.timer)
    guard = RequestDeadline(30, monotonic=clock, should_cancel=lambda: clock.value >= 0.2)
    guard.start()
    waits = []

    class OccupiedSlots:
        def acquire(self, *, timeout):
            waits.append(timeout)
            clock.advance(timeout)
            return False

    def unexpected_thread(**kwargs):
        pytest.fail("DNS resolver started without a slot")

    monkeypatch.setattr(deadline_module, "_DNS_SLOTS", OccupiedSlots())
    monkeypatch.setattr(deadline_module.threading, "Thread", unexpected_thread)
    try:
        with pytest.raises(RequestCancelled):
            deadline_module._resolve("fixture.invalid", 443, guard)
        assert clock.value <= 0.21 and max(waits) <= 0.1
        assert not guard.request_started
    finally:
        guard.close()


def test_cancellation_waiting_for_dns_leaves_no_request_data_in_detached_thread(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(deadline_module.threading, "Timer", clock.timer)
    guard = RequestDeadline(30, monotonic=clock, should_cancel=lambda: clock.value >= 0.2)
    guard.start()
    targets = []

    class PendingThread:
        def __init__(self, *, target, daemon):
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
    monkeypatch.setattr(deadline_module, "_DNS_SLOTS", threading.BoundedSemaphore(4))
    try:
        with pytest.raises(RequestCancelled):
            deadline_module._resolve("fixture.invalid", 443, guard)
        assert clock.value <= 0.21 and not guard.request_started
        assert len(targets) == 1
        assert set(targets[0].__code__.co_freevars) == {"finished", "host", "port", "result"}
    finally:
        guard.close()


@pytest.mark.parametrize("phase", ["dns", "connect", "tls", "send"])
def test_real_urllib_marks_generation_unknown_only_when_http_write_may_have_started(
    monkeypatch, phase
):
    clock, connection = install_clock_and_socket(monkeypatch)
    request_url = LOCAL_URL
    if phase == "dns":
        request_url = "http://fixture.invalid:11434/api/generate"

        def fail_dns(*args, **kwargs):
            raise OSError("fixture DNS failure")

        monkeypatch.setattr(deadline_module.socket, "getaddrinfo", fail_dns)
    elif phase == "connect":

        def fail_connect(address):
            raise OSError("fixture connect failure")

        connection.connect = fail_connect
    elif phase == "send":

        def fail_send(data):
            raise OSError("fixture first write failure")

        connection.sendall = fail_send
    else:
        request_url = LOCAL_URL.replace("http:", "https:")

        class TLSContext:
            def wrap_socket(self, raw, **kwargs):
                def fail_handshake():
                    raise ssl.SSLCertVerificationError("fixture certificate failure")

                raw.do_handshake = fail_handshake
                return raw

        monkeypatch.setattr("narumi.providers.metadata.http.tls_context", TLSContext)
    with pytest.raises(EngineUnavailableError) as failure:
        JSONHTTPClient(monotonic=clock).request(
            "POST", request_url, payload={"prompt": "fixture"}, response_kind="generation"
        )
    assert failure.value.details == (
        {"reason": "provider_generation_outcome_unknown", "outcome_unknown": True}
        if phase == "send"
        else {"reason": "metadata_connection_failed"}
    )
    assert not connection.sent


@contextmanager
def local_http_server(reply, *, read_body=True):
    """Record loopback requests; a stalled upload can opt out of reading its body."""
    requests, failures = [], []
    stop = threading.Event()
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(4)
    listener.settimeout(0.05)
    url = f"http://127.0.0.1:{listener.getsockname()[1]}/generate"

    def serve():
        while not stop.is_set():
            try:
                connection, _ = listener.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            try:
                with connection:
                    connection.settimeout(2)
                    received = b""
                    while b"\r\n\r\n" not in received:
                        part = connection.recv(4096)
                        if not part:
                            raise AssertionError("fixture missing request headers")
                        received += part
                    head, body = received.split(b"\r\n\r\n", 1)
                    length = next(
                        int(line.split(b":", 1)[1])
                        for line in head.split(b"\r\n")
                        if line.lower().startswith(b"content-length:")
                    )
                    while read_body and len(body) < length:
                        part = connection.recv(length - len(body))
                        if not part:
                            raise AssertionError("fixture missing request body")
                        body += part
                    requests.append((head, body))
                    reply(connection)
            except Exception as exc:
                failures.append(type(exc).__name__)

    worker = threading.Thread(target=serve, daemon=True)
    worker.start()
    try:
        yield url, requests
    finally:
        stop.set()
        listener.close()
        worker.join(timeout=3)
        assert not worker.is_alive() and not failures


@pytest.mark.parametrize("phase", ["headers", "body"])
def test_cancel_interrupts_real_blocked_socket_promptly_without_a_second_post(phase):
    cancelled, disconnected = threading.Event(), threading.Event()
    cancelled_at = []

    def cancel():
        cancelled_at.append(time.monotonic())
        cancelled.set()

    def reply(connection):
        if phase == "body":
            connection.sendall(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 20\r\n\r\n{"
            )
        timer = threading.Timer(0.05, cancel)
        timer.start()
        try:
            if connection.recv(1) == b"":
                disconnected.set()
        finally:
            timer.cancel()

    with local_http_server(reply) as (url, requests):
        with pytest.raises(CancelledError) as failure:
            JSONHTTPClient().request(
                "POST",
                url,
                payload={"prompt": "fixture"},
                response_kind="generation",
                timeout=3,
                should_cancel=cancelled.is_set,
            )
        assert time.monotonic() - cancelled_at[0] < 0.5
        assert disconnected.wait(1) and len(requests) == 1
        assert failure.value.details == {
            "reason": "provider_generation_outcome_unknown",
            "outcome_unknown": True,
        }


def test_real_tls_handshake_cancellation_is_prompt_and_known_before_http_send(monkeypatch):
    cancelled, disconnected = threading.Event(), threading.Event()
    cancelled_at, failures = [], []

    # Cross-thread close does not reliably wake a blocking SSL_do_handshake on
    # every supported macOS/OpenSSL combination. Cancellation must instead be
    # observed by the request thread's bounded nonblocking handshake poll.
    monkeypatch.setattr(deadline_module, "_abort", lambda _connection: None)

    def cancel():
        cancelled_at.append(time.monotonic())
        cancelled.set()

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(2)
        url = f"https://127.0.0.1:{listener.getsockname()[1]}/generate"

        def peer():
            try:
                connection, _ = listener.accept()
                with connection:
                    connection.settimeout(2)

                    def receive_exact(size):
                        chunks = []
                        while size:
                            chunk = connection.recv(size)
                            if not chunk:
                                raise OSError("fixture TLS record ended early")
                            chunks.append(chunk)
                            size -= len(chunk)
                        return b"".join(chunks)

                    header = receive_exact(5)
                    assert header.startswith(b"\x16\x03")
                    receive_exact(int.from_bytes(header[3:5], "big"))
                    timer = threading.Timer(0.05, cancel)
                    timer.start()
                    try:
                        if connection.recv(4096) == b"":
                            disconnected.set()
                    finally:
                        timer.cancel()
            except Exception as exc:
                failures.append(type(exc).__name__)

        worker = threading.Thread(target=peer, daemon=True)
        worker.start()
        try:
            with pytest.raises(CancelledError) as failure:
                JSONHTTPClient().request(
                    "POST",
                    url,
                    payload={"prompt": "fixture"},
                    response_kind="generation",
                    timeout=3,
                    should_cancel=cancelled.is_set,
                )
            assert time.monotonic() - cancelled_at[0] < 0.5
            assert disconnected.wait(1)
            assert failure.value.details == {"reason": "provider_generation_cancelled"}
        finally:
            worker.join(timeout=3)
        assert not worker.is_alive() and not failures


@pytest.mark.parametrize("response", [b"", b"HTTP/1.1 500 Failed\r\nContent-Length: 0\r\n\r\n"])
def test_real_generation_disconnect_and_server_error_never_retry(response):
    def reply(connection):
        if response:
            connection.sendall(response)

    with local_http_server(reply) as (url, requests):
        with pytest.raises(EngineUnavailableError) as failure:
            JSONHTTPClient().request(
                "POST", url, payload={"prompt": "fixture"}, response_kind="generation"
            )
        assert len(requests) == 1
        assert failure.value.details == {
            "reason": "provider_generation_outcome_unknown",
            "outcome_unknown": True,
        }


@pytest.mark.parametrize("response_kind", ["metadata", "generation"])
@pytest.mark.parametrize("extra_declared_bytes", [None, 0, 50])
def test_real_response_must_complete_its_declared_content_length(
    response_kind, extra_declared_bytes
):
    body = b'{"response":"fixture summary","done":true}'

    def reply(connection):
        length = (
            b""
            if extra_declared_bytes is None
            else f"Content-Length: {len(body) + extra_declared_bytes}\r\n".encode()
        )
        connection.sendall(
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\n"
            + length
            + b"\r\n"
            + body
        )

    with local_http_server(reply) as (url, requests):
        client = JSONHTTPClient()
        options = {"payload": {"prompt": "fixture"}, "response_kind": response_kind}
        if extra_declared_bytes:
            with pytest.raises(EngineUnavailableError) as failure:
                client.request("POST", url, **options)
            assert failure.value.details == (
                {"reason": "provider_generation_outcome_unknown", "outcome_unknown": True}
                if response_kind == "generation"
                else {"reason": "invalid_metadata"}
            )
        else:
            assert client.request("POST", url, **options) == {
                "response": "fixture summary",
                "done": True,
            }
        assert len(requests) == 1


def test_real_generation_redirect_never_forwards_credentials(monkeypatch):
    forwarded = []
    retained_responses = []
    real_redirect = RejectRedirects.redirect_request

    def observe_redirect(self, req, fp, *args, **kwargs):
        forwarded.append(False)
        retained_responses.append((fp, fp.fp))
        return real_redirect(self, req, fp, *args, **kwargs)

    monkeypatch.setattr(RejectRedirects, "redirect_request", observe_redirect)
    with local_http_server(lambda connection: None) as (destination, destination_requests):

        def reply(connection):
            connection.sendall(
                (
                    f"HTTP/1.1 307 Redirect\r\nLocation: {destination}\r\nContent-Length: 0\r\n\r\n"
                ).encode()
            )

        with local_http_server(reply) as (url, requests):
            with pytest.raises(EngineUnavailableError) as failure:
                JSONHTTPClient().request(
                    "POST",
                    url,
                    payload={"prompt": "fixture"},
                    headers={"Authorization": "Bearer fixture-test-token"},
                    response_kind="generation",
                )
            assert len(requests) == 1 and not destination_requests
            assert failure.value.details["outcome_unknown"] is True
    assert forwarded == [False]
    assert len(retained_responses) == 1
    response, body_file = retained_responses[0]
    assert response.closed and response.fp is None
    assert body_file.closed


class PartialWriteSocket:
    def __init__(self, actions):
        self.actions, self.calls = list(actions), []
        self.accepted = bytearray()
        self.timeout, self.closed = 3, False

    def gettimeout(self):
        return self.timeout

    def settimeout(self, value):
        self.timeout = value

    def setblocking(self, value):
        assert value is False

    def send(self, data):
        self.calls.append(bytes(data))
        action = self.actions.pop(0) if self.actions else len(data)
        if isinstance(action, Exception):
            raise action
        self.accepted.extend(data[:action])
        return action

    def shutdown(self, how):
        self.closed = True

    def close(self):
        self.closed = True


def test_interruptible_tls_write_keeps_the_same_suffix_across_want_read_and_write(monkeypatch):
    connection = PartialWriteSocket([3, ssl.SSLWantWriteError(), ssl.SSLWantReadError(), 2])
    waits = []

    def ready(readers, writers, errors, timeout):
        waits.append((readers, writers, timeout))
        return readers, writers, []

    monkeypatch.setattr(deadline_module.select, "select", ready)
    guard = RequestDeadline(3, interruptible_write=True)
    guard.track(connection)
    request = deadline_module._HTTPConnection("127.0.0.1", deadline=guard)
    request.sock = connection
    payload = b"fixture multipart body"
    try:
        request.send(payload)
        assert bytes(connection.accepted) == payload
        assert connection.calls == [payload, payload[3:], payload[3:], payload[3:], payload[5:]]
        assert waits == [([], [connection], 0.1), ([connection], [], 0.1)]
        assert connection.timeout > 0.1 and guard.request_started
    finally:
        guard.close()
    assert connection.closed


@pytest.mark.parametrize("cancel", [False, True])
def test_interruptible_write_poll_enforces_cancellation_and_absolute_deadline(monkeypatch, cancel):
    clock = Clock()
    monkeypatch.setattr(deadline_module.threading, "Timer", clock.timer)
    connection = PartialWriteSocket([BlockingIOError(), BlockingIOError()])

    def not_ready(readers, writers, errors, timeout):
        clock.advance(timeout)
        return [], [], []

    monkeypatch.setattr(deadline_module.select, "select", not_ready)
    guard = RequestDeadline(
        30 if cancel else 0.2,
        monotonic=clock,
        interruptible_write=True,
        should_cancel=(lambda: clock.value >= 0.2) if cancel else None,
    )
    request = deadline_module._HTTPConnection("127.0.0.1", deadline=guard)
    request.sock = connection
    guard.track(connection)
    guard.start()
    try:
        with pytest.raises(RequestCancelled if cancel else TimeoutError):
            request.send(b"fixture multipart body")
        assert clock.value <= 0.21 and guard.request_started
        assert connection.closed and not connection.accepted
        assert len(connection.calls) == 2
    finally:
        guard.close()
