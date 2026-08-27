"""Product CLI tests: contract-driven commands, in-process execution and the HTTP path.

The HTTP fixture starts a real ``narumi-server`` (uvicorn thread, loopback port) like
``test_transports.py`` does. Contract tools that have no real handler yet are filled with
stub handlers returning their contract output example, so the fixture keeps working while
the server catches up with the contract set.
"""

from __future__ import annotations

import copy
import json
import re
import socket
import threading
import time
from pathlib import Path
from typing import Any

import click
import pytest
import uvicorn
from click.testing import CliRunner, Result
from conftest import make_recorded_bundle
from narumi.contracts import ContractSet, load_contracts
from narumi_server import cli_tools
from narumi_server.app import ToolOutcome, build_server
from narumi_server.context import ServerContext, build_context
from narumi_server.handlers import HANDLERS
from narumi_server.transports import build_http_app

MEETING_A = "20260827T010000Z-0000000a"
UUID4_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")


@pytest.fixture(scope="module")
def contracts() -> ContractSet:
    return load_contracts()


@pytest.fixture(scope="module")
def cli(contracts: ContractSet) -> click.Group:
    return cli_tools.build_cli(contracts)


def invoke(cli: click.Group, args: list[str]) -> Result:
    return CliRunner().invoke(cli, args, catch_exceptions=False)


def get_command(cli: click.Group, name: str) -> click.Command:
    command = cli.get_command(click.Context(cli), name)
    assert command is not None, f"missing subcommand {name!r}"
    return command


def options_of(command: click.Command) -> dict[str, click.Option]:
    return {p.name: p for p in command.params if isinstance(p, click.Option)}


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def unreachable_url() -> str:
    return f"http://127.0.0.1:{free_port()}/mcp"


# ---------------------------------------------------------------------------- command generation
def test_every_contract_tool_has_a_subcommand(cli: click.Group, contracts: ContractSet):
    names = set(cli.list_commands(click.Context(cli)))
    expected = {tool.replace("_", "-") for tool in contracts.tool_names()}
    assert expected <= names
    assert cli_tools.GENERIC_COMMAND in names
    assert names == expected | {cli_tools.GENERIC_COMMAND}


def test_options_cover_every_schema_property(cli: click.Group, contracts: ContractSet):
    for contract in contracts:
        command = get_command(cli, contract.name.replace("_", "-"))
        assert set(options_of(command)) == set(contract.input_schema.get("properties", {}))


def test_write_tools_never_require_request_id_option(cli: click.Group, contracts: ContractSet):
    for contract in contracts:
        properties = contract.input_schema.get("properties", {})
        if "request_id" not in properties:
            continue
        option = options_of(get_command(cli, contract.name.replace("_", "-")))["request_id"]
        assert option.required is False  # auto-generated UUID4 when omitted


def test_generated_option_types_match_schema(cli: click.Group):
    search = options_of(get_command(cli, "search-transcripts"))
    assert search["query"].required is True and search["query"].type is click.STRING
    assert search["limit"].type is click.INT and search["limit"].required is False
    assert search["scope"].type is click.STRING  # scope selector: name or JSON array

    minutes = options_of(get_command(cli, "get-minutes"))
    assert minutes["version"].type is click.INT
    assert minutes["meeting_id"].required is True

    importing = options_of(get_command(cli, "import-recording"))
    assert importing["meeting_name"].required is True
    assert importing["mic_path"].type is click.STRING and importing["mic_path"].required is False
    assert importing["copy"].is_bool_flag and importing["copy"].secondary_opts == ["--no-copy"]
    assert importing["copy"].default is None  # omitted = the contract default, not the CLI's

    deleting = options_of(get_command(cli, "delete-meeting"))
    assert deleting["confirm"].is_bool_flag and deleting["confirm"].required is True

    tracks = options_of(get_command(cli, "discard-tracks"))
    assert tracks["tracks"].type is click.STRING  # array → JSON string
    assert "[JSON]" in (tracks["tracks"].help or "")


# ---------------------------------------------------------------------------- argument assembly
@pytest.fixture
def dispatched(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Capture what reaches ``dispatch`` without touching disk or contracts handlers."""
    seen: dict[str, Any] = {}

    class DummyContext:
        def close(self) -> None:
            seen["closed"] = True

    def fake_build_context(*args: Any, **kwargs: Any) -> DummyContext:
        return DummyContext()

    def fake_dispatch(ctx: Any, tool: str, args: dict[str, Any]) -> ToolOutcome:
        seen["tool"], seen["args"] = tool, dict(args)
        return ToolOutcome(payload={"ok": True}, is_error=False)

    monkeypatch.setattr(cli_tools, "build_context", fake_build_context)
    monkeypatch.setattr(cli_tools, "dispatch", fake_dispatch)
    return seen


def test_request_id_is_autogenerated(cli: click.Group, dispatched: dict[str, Any]):
    result = invoke(cli, ["--in-process", "rebuild-catalog"])
    assert result.exit_code == 0, result.stderr
    assert dispatched["tool"] == "rebuild_catalog"
    assert UUID4_RE.match(dispatched["args"]["request_id"])
    assert dispatched["closed"] is True


def test_explicit_request_id_wins(cli: click.Group, dispatched: dict[str, Any]):
    result = invoke(cli, ["--in-process", "rebuild-catalog", "--request-id", "my-request-01"])
    assert result.exit_code == 0, result.stderr
    assert dispatched["args"]["request_id"] == "my-request-01"


def test_array_option_takes_json(cli: click.Group, dispatched: dict[str, Any]):
    result = invoke(
        cli,
        [
            "--in-process",
            "discard-tracks",
            "--meeting-id",
            MEETING_A,
            "--tracks",
            '["mic", "system"]',
        ],
    )
    assert result.exit_code == 0, result.stderr
    assert dispatched["args"]["tracks"] == ["mic", "system"]
    assert dispatched["args"]["meeting_id"] == MEETING_A


def test_array_option_rejects_non_json(cli: click.Group, dispatched: dict[str, Any]):
    result = invoke(
        cli, ["--in-process", "discard-tracks", "--meeting-id", MEETING_A, "--tracks", "mic"]
    )
    assert result.exit_code == 2
    assert json.loads(result.stderr)["error"]["code"] == "invalid_argument"
    assert "args" not in dispatched  # never dispatched


def test_scope_selector_accepts_name_or_json_array(cli: click.Group, dispatched: dict[str, Any]):
    invoke(cli, ["--in-process", "list-meetings", "--scope", "cloudnative"])
    assert dispatched["args"]["scope"] == "cloudnative"
    invoke(cli, ["--in-process", "list-meetings", "--scope", '["cloudnative", "personal"]'])
    assert dispatched["args"]["scope"] == ["cloudnative", "personal"]


def test_boolean_flag_pair_and_omission(cli: click.Group, dispatched: dict[str, Any]):
    invoke(
        cli,
        [
            "--in-process",
            "import-recording",
            "--meeting-name",
            "取り込み",
            "--mic-path",
            "/tmp/mic.m4a",
            "--no-copy",
        ],
    )
    assert dispatched["args"]["copy"] is False
    invoke(
        cli,
        ["--in-process", "import-recording", "--meeting-name", "取り込み", "--mic-path", "/x.m4a"],
    )
    assert "copy" not in dispatched["args"]  # omitted option → contract default applies serverside


# ---------------------------------------------------------------------------- in-process execution
def test_in_process_get_server_info(cli: click.Group, contracts: ContractSet, home: Path):
    result = invoke(cli, ["--in-process", "get-server-info"])
    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["name"] == "narumi"
    assert payload["contract_version"] == contracts.contract_version


def test_in_process_list_meetings_raw(cli: click.Group, home: Path):
    context = build_context(home, transports=["test"])
    make_recorded_bundle(context, meeting_id=MEETING_A, name="CLI の会議")
    context.close()
    result = invoke(cli, ["--in-process", "--raw", "list-meetings"])
    assert result.exit_code == 0, result.stderr
    assert "\n" not in result.stdout.strip()  # --raw prints one line
    payload = json.loads(result.stdout)
    assert [m["meeting_id"] for m in payload["meetings"]] == [MEETING_A]


@pytest.mark.parametrize("tool", sorted(cli_tools.RECORDING_TOOLS))
def test_recording_tools_are_refused_in_process(cli: click.Group, home: Path, tool: str):
    result = invoke(cli, ["--in-process", tool.replace("_", "-")])
    assert result.exit_code == 2
    envelope = json.loads(result.stderr)
    assert envelope["error"]["code"] == "invalid_argument"
    assert "narumi-server" in envelope["error"]["message"]


def test_in_process_error_envelope_on_stderr(cli: click.Group, home: Path):
    result = invoke(cli, ["--in-process", "get-meeting", "--meeting-id", "nope"])
    assert result.exit_code == 2
    assert result.stdout == ""
    assert json.loads(result.stderr)["error"]["code"] == "invalid_argument"


# ---------------------------------------------------------------------------- escape hatch
def test_json_escape_hatch(cli: click.Group, home: Path):
    result = invoke(cli, ["--in-process", "tool", "list_meetings", "--json", "{}"])
    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout) == {"meetings": []}
    hyphenated = invoke(cli, ["--in-process", "tool", "list-meetings", "--json", "{}"])
    assert hyphenated.exit_code == 0


def test_json_escape_hatch_rejects_bad_json(cli: click.Group, home: Path):
    result = invoke(cli, ["--in-process", "tool", "list_meetings", "--json", "{broken"])
    assert result.exit_code == 2
    assert json.loads(result.stderr)["error"]["code"] == "invalid_argument"
    non_object = invoke(cli, ["--in-process", "tool", "list_meetings", "--json", '["x"]'])
    assert non_object.exit_code == 2
    assert json.loads(non_object.stderr)["error"]["code"] == "invalid_argument"


def test_unknown_tool_exits_2(cli: click.Group, home: Path):
    result = invoke(cli, ["--in-process", "tool", "no_such_tool", "--json", "{}"])
    assert result.exit_code == 2
    envelope = json.loads(result.stderr)
    assert envelope["error"]["code"] == "invalid_argument"
    assert "no_such_tool" in envelope["error"]["message"]


def test_unknown_subcommand_is_a_usage_error(cli: click.Group):
    result = CliRunner().invoke(cli, ["no-such-command"])
    assert result.exit_code == 2  # click usage error, no traceback


# ---------------------------------------------------------------------------- server selection
def test_require_server_fails_when_unreachable(cli: click.Group, home: Path):
    url = unreachable_url()
    result = invoke(cli, ["--require-server", "--server-url", url, "list-meetings"])
    assert result.exit_code == 2
    envelope = json.loads(result.stderr)
    assert envelope["error"]["code"] == "internal"
    assert "not reachable" in envelope["error"]["message"]
    assert envelope["error"]["details"]["server_url"] == url


def test_auto_mode_falls_back_in_process(cli: click.Group, home: Path):
    result = invoke(cli, ["--server-url", unreachable_url(), "list-meetings"])
    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout) == {"meetings": []}


def test_bad_server_url_is_invalid_argument(cli: click.Group, home: Path):
    result = invoke(cli, ["--server-url", "ftp://127.0.0.1/mcp", "list-meetings"])
    assert result.exit_code == 2
    assert json.loads(result.stderr)["error"]["code"] == "invalid_argument"


# ---------------------------------------------------------------------------- HTTP path
def _stub_handler(contract: Any):
    example = contract.output_examples[0] if contract.output_examples else {}

    def handler(ctx: ServerContext, args: dict[str, Any]) -> dict[str, Any]:
        return copy.deepcopy(example)

    return handler


@pytest.fixture
def http_url(home: Path, contracts: ContractSet):
    """A real narumi-server on a loopback port (contract tools without handlers are stubbed)."""
    handlers = dict(HANDLERS)
    for name in contracts.tool_names():
        handlers.setdefault(name, _stub_handler(contracts[name]))
    context = build_context(home, transports=["streamable-http"], handlers=handlers)
    server = build_server(context)
    app = build_http_app(server, host="127.0.0.1")
    port = free_port()
    config = uvicorn.Config(
        app, host="127.0.0.1", port=port, log_config=None, log_level="warning", access_log=False
    )
    http_server = uvicorn.Server(config)
    thread = threading.Thread(target=http_server.run, name="uvicorn-cli-test", daemon=True)
    thread.start()
    try:
        for _ in range(200):
            if http_server.started:
                break
            time.sleep(0.05)
        assert http_server.started, "uvicorn did not start"
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        http_server.should_exit = True
        thread.join(timeout=15)
        context.close()


def test_http_roundtrip(cli: click.Group, http_url: str):
    result = invoke(cli, ["--require-server", "--server-url", http_url, "get-server-info"])
    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["name"] == "narumi"
    assert payload["capabilities"]["transports"] == ["streamable-http"]


def test_http_error_envelope(cli: click.Group, http_url: str):
    result = invoke(
        cli, ["--require-server", "--server-url", http_url, "get-meeting", "--meeting-id", "nope"]
    )
    assert result.exit_code == 2
    assert json.loads(result.stderr)["error"]["code"] == "invalid_argument"
