"""All resident CLI calls use pinned TLS, authenticated sessions and no implicit replay."""

from __future__ import annotations

import json
import socket
import ssl
import sys
import threading
import time
import urllib.request
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from click.testing import CliRunner
from narumi.errors import InvalidArgumentError
from narumi_server import cli_tools
from narumi_server.cli_transport import ConfidentialHttpTransport, confidential_endpoint
from narumi_server.secure_transport import (
    ClientTransport,
    ServerTransport,
    TransportSecurityError,
    load_client_transport,
    prepare_server_transport,
)

SECRET = "fake-cli-tls-key-6380"
GAIA_URL = "http://127.0.0.1:4111/mcp"
PROXY_VARIABLES = (
    "HTTP_PROXY",
    "http_proxy",
    "HTTPS_PROXY",
    "https_proxy",
    "ALL_PROXY",
    "all_proxy",
)


class FakeSecretStore:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def get(self, account: str) -> str | None:
        return self.values.get(account)

    def set(self, account: str, value: str) -> None:
        self.values[account] = value

    def delete(self, account: str) -> None:
        self.values.pop(account, None)


@pytest.fixture(autouse=True)
def isolated_environment(home: Path, monkeypatch: pytest.MonkeyPatch):
    for name in (*PROXY_VARIABLES, "NO_PROXY", "no_proxy", "REQUEST_METHOD", "NARUMI_SERVER_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(urllib.request, "_opener", None)


@dataclass
class Endpoint:
    root: Path
    store: FakeSecretStore
    credentials: ServerTransport | None = None
    requests: list[dict[str, Any]] = field(default_factory=list)
    redirect_label: str | None = None
    redirect_url: str = ""
    redirect_status: int = 307
    failure_label: str | None = None
    failure_status: int = 403
    rpc_error_label: str | None = None
    delay_label: str | None = None
    contract_version: str = "4.0.0"
    protocol_version: str = cli_tools.PROTOCOL_VERSION
    reported_instance: str | None = None

    @property
    def url(self) -> str:
        assert self.credentials is not None
        return self.credentials.url

    @property
    def client(self) -> ClientTransport:
        return load_client_transport(self.root, expected_url=self.url, secret_store=self.store)

    @property
    def labels(self) -> list[str]:
        return [request["label"] for request in self.requests]


@pytest.fixture
def endpoint_factory(home: Path, monkeypatch) -> Iterator[Callable[[], Endpoint]]:
    servers: list[tuple[ThreadingHTTPServer, threading.Thread, Endpoint]] = []
    roots: dict[Path, FakeSecretStore] = {}

    def load(root: Path, *, expected_url: str | None = None) -> ClientTransport:
        return load_client_transport(root, expected_url=expected_url, secret_store=roots[root])

    monkeypatch.setattr(cli_tools, "load_client_transport", load)

    def create() -> Endpoint:
        endpoint = Endpoint(home / f"tls-{len(servers)}", FakeSecretStore())
        roots[endpoint.root] = endpoint.store

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args: Any) -> None:
                pass

            def respond(self) -> None:
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                message = json.loads(body) if body else {}
                label = message.get("method", self.command)
                if label == "tools/call":
                    label = message["params"]["name"]
                endpoint.requests.append(
                    {
                        "label": label,
                        "path": self.path,
                        "host": self.headers["Host"],
                        "authorization": self.headers.get("Authorization"),
                        "body": body,
                    }
                )
                assert endpoint.credentials is not None
                authenticated = self.headers.get("Authorization") == (
                    "Bearer " + endpoint.credentials.client_token
                )
                if not authenticated or label == endpoint.failure_label:
                    self.send_response(endpoint.failure_status)
                    self.end_headers()
                    self.wfile.write(SECRET.encode())
                    return
                if label == endpoint.redirect_label:
                    self.send_response(endpoint.redirect_status)
                    self.send_header("Location", endpoint.redirect_url)
                    self.end_headers()
                    self.wfile.write(SECRET.encode())
                    return
                if label == endpoint.delay_label:
                    time.sleep(0.1)
                if self.command == "DELETE" or "id" not in message:
                    self.send_response(202)
                    self.end_headers()
                    return
                if label == "initialize":
                    result = {"protocolVersion": endpoint.protocol_version, "capabilities": {}}
                else:
                    payload = (
                        {
                            "name": "narumi",
                            "contract_version": endpoint.contract_version,
                            "server_instance_id": endpoint.reported_instance
                            or endpoint.credentials.server_instance_id,
                        }
                        if label == "get_server_info"
                        else {
                            "connection": {"url": GAIA_URL, "has_api_key": True, "source": "saved"}
                        }
                    )
                    result = {"structuredContent": payload, "isError": False}
                response = json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": message["id"],
                        **(
                            {"error": {"code": -32001, "message": SECRET}}
                            if label == endpoint.rpc_error_label
                            else {"result": result}
                        ),
                    }
                ).encode()
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Mcp-Session-Id", "fixture-session")
                    self.send_header("Content-Length", str(len(response)))
                    self.end_headers()
                    self.wfile.write(response)
                except (BrokenPipeError, ConnectionResetError, ssl.SSLError):
                    pass

            do_POST = respond
            do_DELETE = respond

        class QuietServer(ThreadingHTTPServer):
            def handle_error(self, request: Any, client_address: Any) -> None:
                if not isinstance(
                    sys.exception(), (BrokenPipeError, ConnectionResetError, ssl.SSLError)
                ):
                    super().handle_error(request, client_address)

        server = QuietServer(("127.0.0.1", 0), Handler)
        endpoint.credentials = prepare_server_transport(
            endpoint.root, str(uuid4()), port=server.server_port, secret_store=endpoint.store
        )
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.load_cert_chain(
            endpoint.credentials.certificate_path, endpoint.credentials.private_key_path
        )
        server.socket = tls.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(
            target=lambda: server.serve_forever(poll_interval=0.01), daemon=True
        )
        servers.append((server, thread, endpoint))
        thread.start()
        return endpoint

    yield create
    for server, thread, endpoint in reversed(servers):
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        endpoint.credentials.close()


@pytest.fixture
def proxy(endpoint_factory, monkeypatch) -> Endpoint:
    endpoint = endpoint_factory()
    for name in PROXY_VARIABLES:
        monkeypatch.setenv(name, endpoint.url.removesuffix("/mcp"))
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.setenv("no_proxy", "")
    return endpoint


def secret_command(generic: bool = False) -> tuple[list[str], str]:
    if generic:
        return ["tool", "set_gaia_connection", "--json-stdin"], json.dumps(
            {"url": GAIA_URL, "api_key": SECRET}
        )
    return ["set-gaia-connection", "--url", GAIA_URL, "--api-key-stdin"], SECRET + "\n"


def invoke(endpoint: Endpoint, *, generic: bool = False, command: list[str] | None = None):
    args, contents = secret_command(generic)
    return CliRunner().invoke(
        cli_tools.build_cli(),
        ["--data-root", str(endpoint.root), "--server-url", endpoint.url, *(command or args)],
        input=None if command else contents,
        catch_exceptions=False,
    )


@pytest.mark.parametrize("generic", [False, True])
def test_complete_secret_session_bypasses_proxies_and_hostname_resolution(
    endpoint_factory,
    proxy,
    monkeypatch,
    caplog,
    generic,
):
    endpoint = endpoint_factory()
    original = socket.getaddrinfo
    hosts: list[str] = []

    def numeric_only(host, *args, **kwargs):
        hosts.append(host)
        assert host == "127.0.0.1"
        return original(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", numeric_only)
    result = invoke(endpoint, generic=generic)
    assert result.exit_code == 0, result.stderr
    assert endpoint.labels == [
        "initialize",
        "notifications/initialized",
        "get_server_info",
        "set_gaia_connection",
        "DELETE",
    ]
    assert len(hosts) == len(endpoint.requests)
    assert all(request["path"] == "/mcp" for request in endpoint.requests)
    assert all(
        request["authorization"] == "Bearer " + endpoint.credentials.client_token
        for request in endpoint.requests
    )
    assert SECRET.encode() in endpoint.requests[-2]["body"]
    assert proxy.requests == []
    assert SECRET not in result.output + caplog.text
    assert endpoint.credentials.client_token not in result.output + caplog.text


def test_ordinary_tools_also_use_authenticated_proxy_free_transport(endpoint_factory, proxy):
    endpoint = endpoint_factory()
    result = invoke(endpoint, command=["get-server-info"])
    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout)["name"] == "narumi"
    assert proxy.requests == []
    assert all(request["authorization"] for request in endpoint.requests)


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
@pytest.mark.parametrize(
    "label", ["initialize", "notifications/initialized", "set_gaia_connection", "DELETE"]
)
def test_redirects_are_never_followed(endpoint_factory, proxy, caplog, status, label):
    endpoint, destination = endpoint_factory(), endpoint_factory()
    endpoint.redirect_label, endpoint.redirect_status = label, status
    endpoint.redirect_url = destination.url
    result = invoke(endpoint)
    assert result.exit_code == (0 if label == "DELETE" else 2)
    assert label in endpoint.labels
    assert destination.requests == proxy.requests == []
    assert SECRET not in result.output + caplog.text


@pytest.mark.parametrize("status", [401, 403])
@pytest.mark.parametrize("label", ["initialize", "get_server_info", "set_gaia_connection"])
def test_authentication_failures_do_not_fallback_or_resend(
    endpoint_factory, monkeypatch, status, label
):
    endpoint = endpoint_factory()
    endpoint.failure_status, endpoint.failure_label = status, label
    monkeypatch.setattr(cli_tools, "_call_in_process", lambda *_a: pytest.fail("no fallback"))
    result = invoke(endpoint)
    assert result.exit_code == 2
    assert json.loads(result.stderr)["error"]["code"] == "authentication_required"
    assert endpoint.labels.count(label) == 1
    assert SECRET not in result.output


@pytest.mark.parametrize("version", ["1.1.0", "2.0.0", "3.0.0", "5.0.0", "invalid", SECRET])
def test_contract_major_is_checked_before_sending_new_arguments(
    endpoint_factory, monkeypatch, version
):
    endpoint = endpoint_factory()
    endpoint.contract_version = version
    monkeypatch.setattr(cli_tools, "_call_in_process", lambda *_a: pytest.fail("no fallback"))
    result = invoke(endpoint)
    assert result.exit_code == 2
    assert json.loads(result.stderr)["error"]["code"] == "contract_mismatch"
    assert "update narumi.app" in result.stderr
    assert "set_gaia_connection" not in endpoint.labels
    assert all(SECRET.encode() not in request["body"] for request in endpoint.requests)
    assert SECRET not in result.output


@pytest.mark.parametrize("version", ["2099-01-01", "2024-11-05", SECRET])
def test_unsupported_mcp_version_stops_before_notification_or_user_call(endpoint_factory, version):
    endpoint = endpoint_factory()
    endpoint.protocol_version = version
    result = invoke(endpoint)
    assert result.exit_code == 2
    assert endpoint.labels == ["initialize", "DELETE"]
    assert all(SECRET.encode() not in request["body"] for request in endpoint.requests)
    assert SECRET not in result.output


def test_bootstrap_instance_change_blocks_the_operation(endpoint_factory):
    endpoint = endpoint_factory()
    endpoint.reported_instance = str(uuid4())
    result = invoke(endpoint)
    assert result.exit_code == 2
    assert "set_gaia_connection" not in endpoint.labels


@pytest.mark.parametrize("label", ["initialize", "get_server_info", "set_gaia_connection"])
def test_mcp_errors_never_replay_or_echo_response_details(endpoint_factory, monkeypatch, label):
    endpoint = endpoint_factory()
    endpoint.rpc_error_label = label
    monkeypatch.setattr(cli_tools, "_call_in_process", lambda *_a: pytest.fail("no fallback"))
    result = invoke(endpoint)
    assert result.exit_code == 2
    assert endpoint.labels.count(label) == 1
    assert SECRET not in result.output


def test_peer_pin_is_checked_before_token_or_tool_input_is_sent(endpoint_factory, monkeypatch):
    endpoint = endpoint_factory()
    credentials = replace(endpoint.client, certificate_sha256="0" * 64)
    monkeypatch.setattr(cli_tools, "load_client_transport", lambda *_a, **_k: credentials)
    result = invoke(endpoint)
    assert result.exit_code == 2
    assert endpoint.requests == []
    assert SECRET not in result.output


def test_untrusted_tls_certificate_never_receives_an_authentication_header(
    endpoint_factory, monkeypatch
):
    endpoint, other = endpoint_factory(), endpoint_factory()
    credentials = replace(endpoint.client, ssl_context=other.client.ssl_context)
    monkeypatch.setattr(cli_tools, "load_client_transport", lambda *_a, **_k: credentials)
    result = invoke(endpoint)
    assert result.exit_code == 2
    assert json.loads(result.stderr)["error"]["code"] == "authentication_required"
    assert endpoint.requests == []


def test_missing_keychain_token_and_explicit_url_mismatch_never_fallback(
    endpoint_factory, monkeypatch
):
    endpoint = endpoint_factory()
    monkeypatch.setattr(cli_tools, "_call_in_process", lambda *_a: pytest.fail("no fallback"))
    endpoint.store.values.clear()
    assert invoke(endpoint, command=["list-meetings"]).exit_code == 2
    assert endpoint.requests == []
    with pytest.raises(TransportSecurityError):
        load_client_transport(
            endpoint.root, expected_url=endpoint.url + "/else", secret_store=endpoint.store
        )


def test_timeout_after_submission_does_not_repeat_the_secret_mutation(
    endpoint_factory, monkeypatch
):
    endpoint = endpoint_factory()
    endpoint.delay_label = "set_gaia_connection"
    monkeypatch.setattr(cli_tools, "CALL_TIMEOUT", 0.02)
    monkeypatch.setattr(cli_tools, "_call_in_process", lambda *_a: pytest.fail("no fallback"))
    result = invoke(endpoint)
    assert result.exit_code == 2
    assert endpoint.labels.count("set_gaia_connection") == 1
    assert SECRET not in result.output


def test_reinitialization_keeps_the_same_authenticated_transport(endpoint_factory, proxy):
    endpoint = endpoint_factory()
    client = cli_tools.McpHttpClient(endpoint.client)
    try:
        for _ in range(2):
            client.probe()
            client.call_tool("set_gaia_connection", {"url": GAIA_URL, "api_key": SECRET})
    finally:
        client.close()
    assert endpoint.labels.count("initialize") == 2
    assert endpoint.labels.count("set_gaia_connection") == 2
    assert proxy.requests == []


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://127.0.0.1:8765/mcp", "https://127.0.0.1:8765/mcp"),
        ("https://127.9.8.7", "https://127.9.8.7/"),
        ("https://[::1]:8765/mcp", "https://[::1]:8765/mcp"),
    ],
)
def test_confidential_endpoint_accepts_only_numeric_loopback_tls(url, expected):
    assert confidential_endpoint(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "",
        None,
        {"url": SECRET},
        "http://127.0.0.1/mcp",
        "https://localhost/mcp",
        "ftp://127.0.0.1/mcp",
        "file:///tmp/mcp",
        "https://remote.invalid/mcp",
        "https://0.0.0.0/mcp",
        "https://[::]/mcp",
        "https://[::ffff:127.0.0.1]/mcp",
        "https://127.0.0.1:0/mcp",
        "https://127.0.0.1:65536/mcp",
        f"https://{SECRET}@127.0.0.1/mcp",
        f"https://127.0.0.1/mcp?key={SECRET}",
        "https://127.0.0.1/mcp#fragment",
        "https://127.0.0.1\\@remote.invalid/mcp",
        "https://127.0.0.1/mcp\n",
        "https://127.0.0.1%2eexample.com/mcp",
    ],
)
def test_unsafe_endpoints_fail_without_echo(url):
    with pytest.raises(InvalidArgumentError) as caught:
        confidential_endpoint(url)
    assert SECRET not in str(caught.value)


def test_http_and_remote_urls_fail_before_probe_or_fallback(home, monkeypatch):
    monkeypatch.setattr(
        cli_tools, "load_client_transport", lambda *_a, **_k: pytest.fail("no load")
    )
    monkeypatch.setattr(cli_tools, "_call_in_process", lambda *_a: pytest.fail("no fallback"))
    for url in ("http://127.0.0.1:8765/mcp", "https://remote.invalid/mcp"):
        result = CliRunner().invoke(cli_tools.build_cli(), ["--server-url", url, "list-meetings"])
        assert result.exit_code == 2
        assert json.loads(result.stderr)["error"]["code"] == "invalid_argument"


def test_transport_rejects_every_request_to_a_different_url(endpoint_factory):
    endpoint = endpoint_factory()
    transport = ConfidentialHttpTransport(endpoint.client)
    with pytest.raises(InvalidArgumentError):
        transport.open(urllib.request.Request(endpoint.url + "/else"), timeout=1)
    assert endpoint.requests == []
