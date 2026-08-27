"""Credential confinement, redaction, and transport parsing tests against local fake servers."""

from __future__ import annotations

import json
import urllib.error
from pathlib import Path

import pytest
from narumi.errors import ContractMismatchError, InvalidArgumentError, NarumiError
from narumi.gaia import ENV_GAIA_API_KEY, ENV_GAIA_URL, GaiaClient
from narumi.gaia._protocol import extract_response

from .test_gaia_client import FakeGaiaServer, tool_error, tool_ok
from .test_gaia_client import gaia_server as gaia_server

KEY = "gaia_narumi_0123456789abcdef0123456789abcdef"


@pytest.mark.parametrize(
    "url",
    [
        "",
        "  ",
        "https://127.0.0.1/mcp",
        "ftp://127.0.0.1/mcp",
        "file:///tmp/mcp",
        "http://example.com/mcp",
        "http://localhost.example.com/mcp",
        "http://0.0.0.0/mcp",
        "http://192.168.1.2/mcp",
        "http://169.254.169.254/mcp",
        "http://2130706433/mcp",
        "http://127.1/mcp",
        "http://0177.0.0.1/mcp",
        "http://[::]/mcp",
        "http://[::ffff:127.0.0.1]/mcp",
        "http://[::1%25lo0]/mcp",
        "http://user:password@127.0.0.1/mcp",
        "http://user@127.0.0.1/mcp",
        "http://127.0.0.1/mcp?key=secret",
        "http://127.0.0.1/mcp?",
        "http://127.0.0.1/mcp#",
        "http://127.0.0.1/mcp#secret",
        "http://127.0.0.1:65536/mcp",
        "http://127.0.0.1:0/mcp",
        "http://127.0.0.1:bad/mcp",
        "http://127.0.0.1\\@example.com/mcp",
        "http://local\nhost/mcp",
        "\nhttp://127.0.0.1/mcp",
        "http://127.0.0.1/mcp\r\n",
        "\thttp://127.0.0.1/mcp",
        "http://127.0.0.1/パス",
    ],
)
def test_reject_unsafe_or_ambiguous_endpoint(url):
    with pytest.raises(InvalidArgumentError) as exc:
        GaiaClient(url, api_key=KEY)
    assert KEY not in str(exc.value)
    assert url not in str(exc.value) or not url.strip()


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://127.0.0.1:4111/mcp", "http://127.0.0.1:4111/mcp"),
        ("http://localhost:4111/mcp", "http://127.0.0.1:4111/mcp"),
        ("http://127.1.2.3:4111/mcp", "http://127.1.2.3:4111/mcp"),
        ("http://[::1]:4111/mcp", "http://[::1]:4111/mcp"),
    ],
)
def test_accept_only_canonical_loopback_endpoints(url, expected):
    assert GaiaClient(url).url == expected


@pytest.mark.parametrize("key", ["", " ", "a b", "key\n", "\rkey", "\tkey", "非ASCII", 1])
def test_invalid_key_never_appears_in_validation_error(key):
    with pytest.raises(InvalidArgumentError) as exc:
        GaiaClient("http://127.0.0.1:4111/mcp", api_key=key)
    assert "non-empty bearer token" in str(exc.value)


def test_key_cannot_be_embedded_in_endpoint_path():
    with pytest.raises(InvalidArgumentError) as exc:
        GaiaClient(f"http://127.0.0.1:4111/{KEY}/mcp", api_key=KEY)
    assert KEY not in str(exc.value)


def test_auth_header_in_initialize_notification_call_and_retry(gaia_server: FakeGaiaServer):
    gaia_server.tools["get_glossary"] = lambda _: tool_ok({"terms": [], "vocabulary_hints": []})
    client = GaiaClient(gaia_server.url, api_key=KEY)
    client.get_glossary()
    gaia_server.fail_next_call_with_404 = True
    client.get_glossary()
    assert len(gaia_server.headers) == 9
    assert all(headers.get("Authorization") == f"Bearer {KEY}" for headers in gaia_server.headers)
    assert KEY not in repr(client)
    assert KEY not in json.dumps(gaia_server.frames)


def test_no_key_means_no_authorization_header(gaia_server: FakeGaiaServer):
    GaiaClient(gaia_server.url).get_server_info()
    assert all("Authorization" not in header for header in gaia_server.headers)


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_redirects_never_receive_credentials(gaia_server: FakeGaiaServer, status):
    target = FakeGaiaServer()
    target.start()
    try:
        gaia_server.http_status = status
        gaia_server.redirect_url = target.url
        with pytest.raises(NarumiError) as exc:
            GaiaClient(gaia_server.url, api_key=KEY).get_server_info()
        assert exc.value.details["status"] == status
        assert target.frames == []
        assert len(gaia_server.frames) == 1
    finally:
        target.stop()


def test_environment_proxy_never_receives_credentials(gaia_server, monkeypatch):
    proxy = FakeGaiaServer()
    proxy.start()
    try:
        for name in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
            monkeypatch.setenv(name, proxy.url)
        monkeypatch.setenv("no_proxy", "")
        monkeypatch.setenv("NO_PROXY", "")
        client = GaiaClient(gaia_server.url, api_key=KEY)
        client.get_server_info()
        assert proxy.frames == []
        assert gaia_server.headers[0]["Authorization"] == f"Bearer {KEY}"
    finally:
        proxy.stop()


@pytest.mark.parametrize("channel", ["http", "rpc", "tool"])
def test_server_echoed_keys_are_redacted_in_errors(gaia_server, caplog, channel):
    message = f"invalid Bearer {KEY}"
    details = {"nested": [KEY, {"header": f"Bearer {KEY}"}]}
    if channel == "http":
        gaia_server.http_status = 401
        gaia_server.http_body = message.encode()
    elif channel == "rpc":
        gaia_server.rpc_errors["get_server_info"] = {
            "code": -32001,
            "message": message,
            "data": {"code": "unauthorized", "message": message, "details": details},
        }
    else:
        gaia_server.tools["get_server_info"] = lambda _: tool_error(
            "unauthorized", message, details
        )
    with pytest.raises(NarumiError) as exc:
        GaiaClient(gaia_server.url, api_key=KEY).get_server_info()
    assert KEY not in str(exc.value)
    assert KEY not in json.dumps(exc.value.to_payload())
    assert KEY not in caplog.text
    assert "[REDACTED]" in str(exc.value)
    assert exc.value.code == "scope_denied"


def test_redaction_precedes_http_body_truncation(gaia_server: FakeGaiaServer):
    gaia_server.http_status = 401
    gaia_server.http_body = ("prefix" + KEY + "." * 490).encode()
    with pytest.raises(NarumiError) as exc:
        GaiaClient(gaia_server.url, api_key=KEY).get_server_info()
    assert KEY[-10:] not in str(exc.value)


def test_transport_reason_is_redacted(gaia_server, monkeypatch):
    client = GaiaClient(gaia_server.url, api_key=KEY)

    def fail(*args, **kwargs):
        raise urllib.error.URLError(f"cannot connect using {KEY}")

    monkeypatch.setattr(client._transport._opener, "open", fail)
    with pytest.raises(NarumiError) as exc:
        client.get_server_info()
    assert KEY not in str(exc.value)
    assert "[REDACTED]" in str(exc.value)


def test_server_info_echoed_key_is_redacted_even_in_allowed_metadata_fields(gaia_server):
    gaia_server.info["client"]["name"] = KEY
    gaia_server.info["client"]["default_scope"] = f"scope-{KEY}"
    gaia_server.info["extra"] = {"api_key": KEY}
    client = GaiaClient(gaia_server.url, api_key=KEY)
    info = client.require_capabilities("search_context")
    assert KEY not in json.dumps(info)
    assert info["client"]["name"] == "[REDACTED]"


def test_invalid_peer_session_header_cannot_echo_key_into_exception(gaia_server):
    gaia_server.session_id = "invalid\r\n " + KEY
    with pytest.raises(ContractMismatchError) as exc:
        GaiaClient(gaia_server.url, api_key=KEY).get_server_info()
    assert KEY not in str(exc.value)


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_non_finite_json_numbers_are_rejected(constant):
    payload = ('{"jsonrpc":"2.0","id":1,"result":{"value":' + constant + "}}").encode()
    with pytest.raises(ContractMismatchError):
        extract_response(payload, "application/json", 1)


def test_env_with_only_a_key_remains_optional(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("NARUMI_HOME", str(tmp_path))
    monkeypatch.delenv(ENV_GAIA_URL, raising=False)
    monkeypatch.setenv(ENV_GAIA_API_KEY, KEY)
    assert GaiaClient.from_env() is None


@pytest.mark.parametrize(
    "data",
    [b"not json", b"[]", b'{"jsonrpc":"2.0","id":true}', b'{"jsonrpc":"1.0","id":1,"result":{}}'],
)
def test_invalid_json_rpc_responses_raise_contract_mismatch(data):
    with pytest.raises(ContractMismatchError):
        extract_response(data, "application/json", 1)


def test_json_batch_and_sse_notifications_are_ignored():
    response = {"jsonrpc": "2.0", "id": 2, "result": {"ok": True}}
    notification = {"jsonrpc": "2.0", "method": "notifications/progress"}
    batch = json.dumps([notification, response]).encode()
    assert extract_response(batch, "application/json", 2) == response
    sse = (
        ": heartbeat\r\n\r\n"
        f"event: message\r\ndata: {json.dumps(notification)}\r\n\r\n"
        'data: {"jsonrpc": "2.0",\r\n'
        'data: "id": 2, "result": {"ok": true}}\r\n\r\n'
    ).encode()
    assert extract_response(sse, "text/event-stream", 2) == response
