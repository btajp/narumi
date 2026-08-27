"""Write-only CLI inputs stay on the pinned MCP endpoint, even with hostile proxies."""

from __future__ import annotations

import json
import socket
import threading
import urllib.request
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import click
import pytest
from click.testing import CliRunner
from narumi.contracts import ContractSet, load_contracts
from narumi.errors import InvalidArgumentError
from narumi_server import cli_tools
from narumi_server.cli_transport import ConfidentialHttpTransport, confidential_endpoint

SECRET = "fake-cli-http-key-6380"
GAIA_URL = "http://127.0.0.1:4111/mcp"
PROXY_VARIABLES = (
    "HTTP_PROXY",
    "http_proxy",
    "HTTPS_PROXY",
    "https_proxy",
    "ALL_PROXY",
    "all_proxy",
)


@pytest.fixture(autouse=True)
def isolated_environment(home: Path, monkeypatch: pytest.MonkeyPatch):
    for name in (*PROXY_VARIABLES, "NO_PROXY", "no_proxy", "REQUEST_METHOD"):
        monkeypatch.delenv(name, raising=False)
    # urlopen caches its default opener: each test must observe this test's environment.
    monkeypatch.setattr(urllib.request, "_opener", None)


@pytest.fixture(scope="module")
def cli() -> click.Group:
    return cli_tools.build_cli()


@dataclass
class Endpoint:
    url: str = ""
    requests: list[dict[str, Any]] = field(default_factory=list)
    redirect_label: str | None = None
    redirect_url: str = ""
    redirect_status: int = 307

    @property
    def labels(self) -> list[str]:
        return [request["label"] for request in self.requests]


@pytest.fixture
def endpoint_factory() -> Iterator[Callable[[], Endpoint]]:
    servers: list[tuple[ThreadingHTTPServer, threading.Thread]] = []

    def create() -> Endpoint:
        endpoint = Endpoint()

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
                    {"label": label, "path": self.path, "host": self.headers["Host"], "body": body}
                )
                if label == endpoint.redirect_label:
                    self.send_response(endpoint.redirect_status)
                    self.send_header("Location", endpoint.redirect_url)
                    self.end_headers()
                    self.wfile.write(SECRET.encode())
                    return
                if self.command == "DELETE" or "id" not in message:
                    self.send_response(202)
                    self.end_headers()
                    return
                if label == "initialize":
                    result = {"protocolVersion": cli_tools.PROTOCOL_VERSION, "capabilities": {}}
                else:
                    payload = (
                        {"name": "narumi"}
                        if label == "get_server_info"
                        else {"url": GAIA_URL, "has_api_key": True, "source": "saved"}
                    )
                    result = {"structuredContent": payload, "isError": False}
                response = json.dumps(
                    {"jsonrpc": "2.0", "id": message["id"], "result": result}
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Mcp-Session-Id", "fixture-session")
                self.send_header("Content-Length", str(len(response)))
                self.end_headers()
                self.wfile.write(response)

            do_POST = respond
            do_DELETE = respond
            do_GET = respond

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        endpoint.url = f"http://127.0.0.1:{server.server_port}/mcp"
        thread = threading.Thread(
            target=lambda: server.serve_forever(poll_interval=0.01), daemon=True
        )
        servers.append((server, thread))
        thread.start()
        return endpoint

    yield create
    for server, thread in reversed(servers):
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.fixture
def proxy(endpoint_factory: Callable[[], Endpoint], monkeypatch: pytest.MonkeyPatch) -> Endpoint:
    endpoint = endpoint_factory()
    for name in PROXY_VARIABLES:
        monkeypatch.setenv(name, endpoint.url.removesuffix("/mcp"))
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.setenv("no_proxy", "")
    return endpoint


def secret_command(generic: bool = False, *, tool: str = "set_gaia_connection") -> list[str]:
    if generic:
        return ["tool", tool, "--json", json.dumps({"url": GAIA_URL, "api_key": SECRET})]
    return [tool.replace("_", "-"), "--url", GAIA_URL, "--api-key", SECRET]


@pytest.mark.parametrize("generic", [False, True])
def test_complete_secret_session_bypasses_environment_proxy_and_dns(
    cli: click.Group,
    endpoint_factory: Callable[[], Endpoint],
    proxy: Endpoint,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    generic: bool,
):
    endpoint = endpoint_factory()
    original_getaddrinfo = socket.getaddrinfo
    hosts: list[str] = []

    def numeric_only(host: str, *args: Any, **kwargs: Any):
        hosts.append(host)
        assert host == "127.0.0.1", "a confidential connection must not resolve a hostname"
        return original_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", numeric_only)
    result = CliRunner().invoke(
        cli,
        [
            "--require-server",
            "--server-url",
            endpoint.url.replace("127.0.0.1", "localhost"),
            *secret_command(generic),
        ],
        catch_exceptions=False,
    )
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
    assert all(request["host"].startswith("127.0.0.1:") for request in endpoint.requests)
    assert SECRET.encode() in endpoint.requests[-2]["body"]
    assert proxy.requests == []
    assert SECRET not in result.stdout + result.stderr + caplog.text


def test_protection_comes_from_write_only_contract_not_a_hardcoded_tool_name(
    endpoint_factory: Callable[[], Endpoint], proxy: Endpoint
):
    source = load_contracts()
    contract = replace(source["set_gaia_connection"], name="secret_probe")
    contracts = ContractSet(
        name=source.name,
        contract_version=source.contract_version,
        tools={contract.name: contract},
        defs=source.defs,
    )
    endpoint = endpoint_factory()
    result = CliRunner().invoke(
        cli_tools.build_cli(contracts),
        ["--require-server", "--server-url", endpoint.url, *secret_command(tool=contract.name)],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.stderr
    assert "secret_probe" in endpoint.labels
    assert proxy.requests == []


@pytest.mark.parametrize("require_server", [False, True])
@pytest.mark.parametrize(
    "url", ["http://remote.invalid/mcp", "https://127.0.0.1/mcp", f"http://{SECRET}@localhost/mcp"]
)
def test_secret_remote_or_credentialed_endpoint_fails_before_probe_or_fallback(
    cli: click.Group, proxy: Endpoint, home: Path, require_server: bool, url: str
):
    result = CliRunner().invoke(
        cli,
        [*(["--require-server"] if require_server else []), "--server-url", url, *secret_command()],
        catch_exceptions=False,
    )
    assert result.exit_code == 2
    assert json.loads(result.stderr)["error"]["code"] == "invalid_argument"
    assert SECRET not in result.stdout + result.stderr
    assert proxy.requests == []
    assert not (home / "gaia.json").exists()


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
@pytest.mark.parametrize(
    "label", ["initialize", "notifications/initialized", "set_gaia_connection", "DELETE"]
)
def test_secret_session_never_follows_redirects(
    cli: click.Group,
    endpoint_factory: Callable[[], Endpoint],
    proxy: Endpoint,
    caplog: pytest.LogCaptureFixture,
    status: int,
    label: str,
):
    endpoint, destination = endpoint_factory(), endpoint_factory()
    endpoint.redirect_label = label
    endpoint.redirect_status = status
    endpoint.redirect_url = destination.url
    result = CliRunner().invoke(
        cli,
        ["--require-server", "--server-url", endpoint.url, *secret_command()],
        catch_exceptions=False,
    )
    assert result.exit_code == (0 if label == "DELETE" else 2)
    assert label in endpoint.labels
    assert destination.requests == []
    assert proxy.requests == []
    assert SECRET not in result.stdout + result.stderr + caplog.text


def test_reinitialization_and_followup_calls_keep_the_private_transport(
    endpoint_factory: Callable[[], Endpoint], proxy: Endpoint
):
    endpoint = endpoint_factory()
    client = cli_tools.McpHttpClient(endpoint.url, confidential=True)
    try:
        for _ in range(2):
            client.probe()
            client.call_tool("set_gaia_connection", {"url": GAIA_URL, "api_key": SECRET})
    finally:
        client.close()
    assert endpoint.labels == [
        "initialize",
        "notifications/initialized",
        "get_server_info",
        "set_gaia_connection",
        "initialize",
        "notifications/initialized",
        "get_server_info",
        "set_gaia_connection",
        "DELETE",
    ]
    assert proxy.requests == []


def test_ordinary_tool_keeps_remote_environment_proxy_route(cli: click.Group, proxy: Endpoint):
    result = CliRunner().invoke(
        cli,
        ["--require-server", "--server-url", "http://remote.invalid/mcp", "get-server-info"],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout)["name"] == "narumi"
    assert proxy.labels == [
        "initialize",
        "notifications/initialized",
        "get_server_info",
        "get_server_info",
        "DELETE",
    ]
    assert all(request["path"] == "http://remote.invalid/mcp" for request in proxy.requests)
    assert all(request["host"] == "remote.invalid" for request in proxy.requests)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://localhost:8765/mcp", "http://127.0.0.1:8765/mcp"),
        ("HTTP://LOCALHOST/mcp", "http://127.0.0.1/mcp"),
        ("http://127.9.8.7", "http://127.9.8.7/"),
        ("http://[::1]:8765/mcp", "http://[::1]:8765/mcp"),
        ("http://[0:0:0:0:0:0:0:1]/mcp", "http://[::1]/mcp"),
    ],
)
def test_confidential_endpoint_pins_only_numeric_loopback(url: str, expected: str):
    assert confidential_endpoint(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "",
        None,
        {"url": SECRET},
        "https://127.0.0.1/mcp",
        "ftp://localhost/mcp",
        "file:///tmp/mcp",
        "http://remote.invalid/mcp",
        "http://localhost.remote.invalid/mcp",
        "http://localhost./mcp",
        "http://0.0.0.0/mcp",
        "http://192.168.1.1/mcp",
        "http://[::]/mcp",
        "http://[::ffff:127.0.0.1]/mcp",
        "http://[::1%25lo0]/mcp",
        "http://[::1/mcp",
        "http://2130706433/mcp",
        "http://127.1/mcp",
        "http://0177.0.0.1/mcp",
        "http://127.0.0.1:0/mcp",
        "http://127.0.0.1:65536/mcp",
        "http://127.0.0.1:bad/mcp",
        f"http://{SECRET}@127.0.0.1/mcp",
        "http://@127.0.0.1/mcp",
        "http://%6cocalhost/mcp",
        f"http://localhost/mcp?key={SECRET}",
        f"http://localhost/mcp#{SECRET}",
        "http://localhost/mcp?",
        "http://localhost/mcp#",
        "http://localhost\\@remote.invalid/mcp",
        " http://localhost/mcp",
        "http://localhost/mcp\n",
        "http://local\thost/mcp",
        "http://localhost/会議",
    ],
)
def test_confidential_endpoint_rejects_unsafe_urls_without_echoing_input(url: Any):
    with pytest.raises(InvalidArgumentError) as caught:
        confidential_endpoint(url)
    assert SECRET not in str(caught.value.to_payload())
    assert caught.value.details == {}


def test_private_transport_rejects_retargeted_requests():
    transport = ConfidentialHttpTransport("http://localhost:8765/mcp")
    with pytest.raises(InvalidArgumentError):
        transport.open(urllib.request.Request("http://remote.invalid/mcp"), timeout=0.01)
