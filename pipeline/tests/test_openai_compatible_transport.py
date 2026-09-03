"""Custom endpoint DNS is approved once and the approved IP set is reused for connect."""

from __future__ import annotations

import socket
import threading
import time
from types import SimpleNamespace

import pytest
from narumi.errors import CancelledError, InvalidArgumentError
from narumi.providers.metadata import deadline as deadline_module
from narumi.providers.metadata.deadline import (
    DeadlineHTTPSHandler,
    RequestDeadline,
    _resolve,
    resolve_addresses,
)
from narumi.providers.metadata.endpoints import (
    is_loopback_endpoint,
    resolve_openai_compatible_addresses,
    validate_openai_compatible_endpoint,
)
from narumi.providers.metadata.openai_compatible_transport import (
    OpenAICompatibleTransport,
    configuration,
)

PUBLIC_V4 = "8.8.8.8"
PUBLIC_V6 = "2606:4700:4700::1111"


class FakeHTTP:
    def __init__(self):
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return {"object": "list", "data": []}


def test_endpoint_canonicalization_preserves_prefix_and_removes_default_port_only():
    assert (
        validate_openai_compatible_endpoint("https://API.Example.test:443/prefix/v1")
        == "https://api.example.test/prefix/v1"
    )
    assert (
        validate_openai_compatible_endpoint("https://api.example.test:8443/prefix/v1")
        == "https://api.example.test:8443/prefix/v1"
    )
    assert validate_openai_compatible_endpoint("http://127.1.2.3:8080/v1") == (
        "http://127.1.2.3:8080/v1"
    )


@pytest.mark.parametrize(
    ("endpoint", "expected"),
    [
        ("http://127.0.0.1:8080/v1", True),
        ("https://[::1]:8080/v1", True),
        ("https://8.8.8.8/v1", False),
        ("https://api.example.test/v1", False),
        ("not-a-url", False),
    ],
)
def test_loopback_helper_is_numeric_only(endpoint, expected):
    assert is_loopback_endpoint(endpoint) is expected


def test_resolution_returns_every_unique_public_address_for_pinning():
    def resolver(host, port, *, type):
        assert (host, port, type) == ("api.example.test", 443, socket.SOCK_STREAM)
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC_V4, port)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC_V4, port)),
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", (PUBLIC_V6, port, 0, 0)),
        ]

    assert resolve_openai_compatible_addresses(
        "https://api.example.test/v1", resolver=resolver
    ) == (PUBLIC_V4, PUBLIC_V6)


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.1",
        "169.254.169.254",
        "192.168.1.1",
        "224.0.0.1",
        "0.0.0.0",
        "::1",
        "fe80::1",
        "fc00::1",
    ],
)
def test_remote_dns_rejects_private_loopback_link_local_multicast_and_unspecified(address):
    family = socket.AF_INET6 if ":" in address else socket.AF_INET

    def resolver(_host, port, *, type):
        target = (address, port, 0, 0) if family == socket.AF_INET6 else (address, port)
        return [(family, socket.SOCK_STREAM, 6, "", target)]

    with pytest.raises(InvalidArgumentError):
        resolve_openai_compatible_addresses("https://api.example.test/v1", resolver=resolver)


def test_pinned_resolve_never_performs_a_second_dns_lookup(monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("a pinned connect must not resolve the hostname again")

    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    deadline = RequestDeadline(1.0)
    deadline.start()
    try:
        infos = _resolve(
            "api.example.test",
            443,
            deadline,
            resolved_addresses=(PUBLIC_V4, PUBLIC_V6),
        )
    finally:
        deadline.close()
    assert [item[4][0] for item in infos] == [PUBLIC_V4, PUBLIC_V6]


def test_https_handler_forwards_pinned_ips_while_tls_keeps_original_sni(monkeypatch):
    observed = {}

    class Socket:
        def setblocking(self, value):
            observed["blocking"] = value

        def settimeout(self, value):
            observed["timeout"] = value

        def do_handshake(self):
            observed["handshake"] = True

        def shutdown(self, _how):
            pass

        def close(self):
            pass

    class Context:
        def wrap_socket(self, raw, *, server_hostname, do_handshake_on_connect):
            observed.update(
                raw=raw,
                server_hostname=server_hostname,
                do_handshake_on_connect=do_handshake_on_connect,
            )
            return Socket()

    raw = Socket()

    def pinned_connect(host, port, deadline, *, resolved_addresses):
        observed.update(host=host, port=port, resolved_addresses=resolved_addresses)
        return raw

    def forbidden_dns(*_args, **_kwargs):
        pytest.fail("pinned HTTPS connection must not use system DNS")

    monkeypatch.setattr(deadline_module, "_connect", pinned_connect)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden_dns)
    guard = RequestDeadline(1.0)
    guard.start()
    handler = DeadlineHTTPSHandler(context=Context())
    request = SimpleNamespace(
        narumi_deadline=guard,
        narumi_resolved_addresses=(PUBLIC_V4,),
    )

    def do_open(factory, forwarded, *, context):
        assert forwarded is request and context is handler._context
        connection = factory("api.example.test", 443, context=context)
        connection.connect()
        return "opened"

    handler.do_open = do_open
    try:
        assert handler.https_open(request) == "opened"
    finally:
        guard.close()
    assert observed["host"] == observed["server_hostname"] == "api.example.test"
    assert observed["port"] == 443
    assert observed["resolved_addresses"] == (PUBLIC_V4,)
    assert observed["do_handshake_on_connect"] is False
    assert observed["blocking"] is False
    assert observed["handshake"] is True


def test_bounded_resolver_fails_closed_on_invalid_results():
    def invalid(_host, port, *, type):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("not-an-ip", port))]

    with pytest.raises(OSError):
        resolve_addresses("api.example.test", 443, timeout=1.0, resolver=invalid)


def test_more_than_sixteen_dns_answers_are_rejected_without_ignoring_the_tail():
    def too_many(_host, port, *, type):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (f"8.8.8.{index}", port))
            for index in range(1, 18)
        ]

    with pytest.raises(OSError):
        resolve_addresses("api.example.test", 443, timeout=1.0, resolver=too_many)


def test_protocol_settings_are_closed_and_never_inferred():
    responses = configuration(
        "https://api.example.test/v1",
        auth_method="api_key",
        api_surface="responses",
    )
    assert responses.chat_max_tokens_field is None
    chat = configuration(
        "http://127.0.0.1:8080/v1",
        auth_method="none",
        api_surface="chat_completions",
        chat_max_tokens_field="max_completion_tokens",
    )
    assert chat.chat_max_tokens_field == "max_completion_tokens"
    for kwargs in (
        {"auth_method": "ambient", "api_surface": "responses"},
        {"auth_method": "api_key", "api_surface": "automatic"},
        {
            "auth_method": "api_key",
            "api_surface": "responses",
            "chat_max_tokens_field": "max_tokens",
        },
        {"auth_method": "api_key", "api_surface": "chat_completions"},
        {
            "auth_method": "api_key",
            "api_surface": "chat_completions",
            "chat_max_tokens_field": "max_output_tokens",
        },
        {"auth_method": "none", "api_surface": "responses"},
    ):
        endpoint = (
            "https://api.example.test/v1"
            if kwargs["auth_method"] != "none"
            else "https://api.example.test/v1"
        )
        with pytest.raises(InvalidArgumentError):
            configuration(endpoint, **kwargs)


@pytest.mark.parametrize(
    ("method", "route", "response_kind", "payload"),
    [
        ("POST", "/admin/delete", "generation", {}),
        ("GET", "/responses", "metadata", None),
        ("POST", "/models", "metadata", {}),
        ("POST", "/chat/completions", "generation", {}),
        ("GET", "/models", "generation", None),
    ],
)
def test_transport_allows_only_models_get_and_selected_surface_post(
    method, route, response_kind, payload
):
    http = FakeHTTP()
    transport = OpenAICompatibleTransport(http=http)
    config = configuration(
        "https://api.example.test/v1",
        auth_method="api_key",
        api_surface="responses",
    )
    with pytest.raises(InvalidArgumentError):
        transport.request(
            config,
            "fixture-key",
            method,
            route,
            payload=payload,
            timeout=10,
            response_kind=response_kind,
        )
    assert http.calls == []


def test_dns_wait_honors_generation_cancellation_before_http():
    release = threading.Event()

    def blocked(_host, port, *, type):
        release.wait(1.0)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC_V4, port))]

    http = FakeHTTP()
    transport = OpenAICompatibleTransport(http=http, resolver=blocked)
    config = configuration(
        "https://api.example.test/v1",
        auth_method="api_key",
        api_surface="responses",
    )
    started = time.monotonic()
    try:
        with pytest.raises(CancelledError):
            transport.request(
                config,
                "fixture-key",
                "POST",
                "/responses",
                payload={"model": "fixture"},
                timeout=10,
                response_kind="generation",
                should_cancel=lambda: time.monotonic() - started >= 0.05,
            )
    finally:
        release.set()
    assert time.monotonic() - started < 0.5
    assert http.calls == []


def test_dns_elapsed_time_is_removed_from_http_budget():
    clock = [100.0]

    def elapsed(_host, port, *, type):
        clock[0] += 3.0
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC_V4, port))]

    http = FakeHTTP()
    transport = OpenAICompatibleTransport(http=http, resolver=elapsed, monotonic=lambda: clock[0])
    config = configuration(
        "https://api.example.test/v1",
        auth_method="api_key",
        api_surface="responses",
    )
    transport.request(
        config,
        "fixture-key",
        "GET",
        "/models",
        timeout=10,
        response_kind="metadata",
    )
    assert http.calls[0][2]["timeout"] == 7.0
