"""CLI input errors for write-only tools cannot reveal raw JSON, keys or parser diagnostics."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import click
import pytest
from click.testing import CliRunner, Result
from narumi.contracts import ContractSet, load_contracts
from narumi.errors import InvalidArgumentError, NarumiError
from narumi_server import cli_tools

SECRET = "fake-cli-credential-916845"
PUBLIC = "ordinary-invalid-input-123"
URL = "http://127.0.0.1:4111/mcp"
VALID_JSON = json.dumps({"url": URL, "api_key": SECRET})
INVALID_JSON = [
    '{"api_key": "' + SECRET + '"',
    "{" + json.dumps(SECRET) + ': {"nested": "' + SECRET + '"}',
    json.dumps([{SECRET: {"api_key": SECRET}}]),
    json.dumps(SECRET),
    json.dumps(SECRET) + " trailing",
    "[" * 1500 + json.dumps({SECRET: SECRET}) + "]" * 1500,
]


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("NARUMI_HOME", str(tmp_path / "data"))
    for name in ("NARUMI_GAIA_URL", "NARUMI_GAIA_API_KEY", "NARUMI_CONTRACTS_DIR"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def cli() -> click.Group:
    return cli_tools.build_cli()


def invoke(cli: click.Group, args: list[str]) -> Result:
    return CliRunner().invoke(cli, args, catch_exceptions=False)


def assert_private_error(result: Result, code: str = "invalid_argument") -> None:
    assert result.exit_code == 2
    assert result.stdout == ""
    assert SECRET not in result.output
    assert SECRET not in result.stderr
    assert json.loads(result.stderr)["error"]["code"] == code


@pytest.mark.parametrize("name", ["set_gaia_connection", "set-gaia-connection"])
@pytest.mark.parametrize("payload", INVALID_JSON)
def test_generic_json_parse_and_top_level_type_failures_are_private(
    cli: click.Group,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    name: str,
    payload: str,
):
    caplog.set_level(logging.DEBUG)
    calls: list[Any] = []
    monkeypatch.setattr(cli_tools, "_call", lambda *args: calls.append(args))
    result = invoke(cli, ["--in-process", "tool", name, "--json", payload])
    assert_private_error(result)
    assert not calls
    assert SECRET not in caplog.text


@pytest.mark.parametrize(
    "arguments",
    [
        {"api_key": {SECRET: SECRET}},
        {"api_key": SECRET * 300},
        {"api_key": SECRET, SECRET: {SECRET: SECRET}},
        {"url": {SECRET: SECRET}, "api_key": SECRET},
    ],
)
def test_well_formed_json_with_invalid_secret_shapes_remains_private(
    cli: click.Group, caplog: pytest.LogCaptureFixture, arguments: dict[str, Any]
):
    caplog.set_level(logging.DEBUG)
    result = invoke(
        cli, ["--in-process", "tool", "set_gaia_connection", "--json", json.dumps(arguments)]
    )
    assert_private_error(result)
    assert SECRET not in caplog.text


@pytest.mark.parametrize(
    "args",
    [
        ["set-gaia-connection", "--api-key", SECRET, SECRET],
        ["set-gaia-connection", "--api-key", SECRET, "--" + SECRET],
        ["set-gaia-connection", "--url", URL, "--api-key"],
        ["tool", "set_gaia_connection", "--json", VALID_JSON, SECRET],
        ["tool", "set_gaia_connection", "--json", VALID_JSON, "--" + SECRET],
        ["tool", "--json", VALID_JSON, "set-gaia-connection", "--" + SECRET],
        ["tool", "--json=" + VALID_JSON, "set_gaia_connection", SECRET],
        ["tool", "--" + SECRET, "set_gaia_connection", "--json", VALID_JSON],
        ["--" + SECRET, "set-gaia-connection", "--api-key", SECRET],
        ["--", "set-gaia-connection", "--api-key", SECRET, "--", SECRET],
    ],
)
def test_click_unknown_options_extra_values_and_missing_values_are_private(
    cli: click.Group, args: list[str]
):
    result = invoke(cli, ["--in-process", *args])
    assert_private_error(result)


def test_global_path_type_failure_is_private(cli: click.Group, tmp_path: Path):
    file_path = tmp_path / SECRET
    file_path.write_text("not a directory")
    result = invoke(
        cli,
        ["--data-root", str(file_path), "set-gaia-connection", "--api-key", SECRET],
    )
    assert_private_error(result)


def test_invalid_server_url_does_not_echo_secret_tool_input(cli: click.Group):
    result = invoke(
        cli,
        ["--server-url", SECRET, "set-gaia-connection", "--url", URL, "--api-key", SECRET],
    )
    assert_private_error(result)


@pytest.mark.parametrize("option", ["count", "config"])
def test_click_and_json_option_errors_follow_nested_write_only_annotation(option: str):
    contracts = load_contracts()
    source = contracts["set_gaia_connection"]
    probe = replace(
        source,
        name="write_secret_probe",
        input_schema={
            "type": "object",
            "properties": {
                "count": {"type": "integer"},
                "config": {"type": "object"},
                "credentials": {
                    "type": "object",
                    "properties": {"key": {"type": "string", "writeOnly": True}},
                },
            },
        },
    )
    isolated = ContractSet(
        name=contracts.name,
        contract_version=contracts.contract_version,
        tools={probe.name: probe},
        defs=contracts.defs,
    )
    cli = cli_tools.build_cli(isolated)
    value = SECRET if option == "count" else '{"' + SECRET + '": "' + SECRET + '"'
    result = invoke(cli, ["--in-process", "write-secret-probe", "--" + option, value])
    assert_private_error(result)


@pytest.mark.parametrize("failure", ["structured", "unexpected", "remote"])
def test_secret_tool_execution_failures_do_not_echo_credentials(
    cli: click.Group, monkeypatch: pytest.MonkeyPatch, failure: str
):
    def fail(*_args):
        if failure == "structured":
            raise NarumiError(SECRET, code=SECRET, details={SECRET: SECRET})
        if failure == "unexpected":
            raise ValueError({SECRET: [SECRET]})
        return {"error": {"code": "internal", "message": SECRET, "details": {SECRET: SECRET}}}, True

    monkeypatch.setattr(cli_tools, "_call", fail)
    result = invoke(cli, ["--in-process", "tool", "set_gaia_connection", "--json", VALID_JSON])
    assert_private_error(result, "internal")


@pytest.mark.parametrize("generic", [False, True])
def test_valid_secret_updates_still_succeed_without_echo(
    cli: click.Group, tmp_path: Path, generic: bool
):
    args = (
        ["tool", "set_gaia_connection", "--json", VALID_JSON]
        if generic
        else ["set-gaia-connection", "--url", URL, "--api-key", SECRET]
    )
    result = invoke(cli, ["--in-process", "--raw", *args])
    assert result.exit_code == 0
    assert SECRET not in result.output
    assert SECRET not in result.stderr
    assert json.loads(result.stdout) == {
        "connection": {"url": URL, "has_api_key": True, "source": "saved"}
    }
    assert json.loads((tmp_path / "data" / "gaia.json").read_text())["api_key"] == SECRET


@pytest.mark.parametrize("payload", ['{"public": "' + PUBLIC + '"', json.dumps([PUBLIC])])
def test_non_secret_json_diagnostics_keep_original_value(cli: click.Group, payload: str):
    result = invoke(cli, ["--in-process", "tool", "list_meetings", "--json", payload])
    assert result.exit_code == 2
    assert json.loads(result.stderr)["error"]["details"]["value"] == payload


@pytest.mark.parametrize(
    "args",
    [
        ["list-meetings", "--" + PUBLIC],
        ["list-meetings", PUBLIC],
        ["search-transcripts", "--query", "q", "--limit", PUBLIC],
        ["tool", "list_meetings", "--json", "{}", "--" + PUBLIC],
    ],
)
def test_non_secret_click_diagnostics_are_unchanged(cli: click.Group, args: list[str]):
    result = invoke(cli, ["--in-process", *args])
    assert result.exit_code == 2
    assert PUBLIC in result.stderr
    assert "Usage:" in result.stderr


def test_non_secret_execution_errors_keep_existing_details(
    cli: click.Group, monkeypatch: pytest.MonkeyPatch
):
    def fail(*_args):
        raise InvalidArgumentError(PUBLIC, details={"value": PUBLIC})

    monkeypatch.setattr(cli_tools, "_call", fail)
    result = invoke(cli, ["--in-process", "list-meetings"])
    error = json.loads(result.stderr)["error"]
    assert error["message"] == PUBLIC
    assert error["details"]["value"] == PUBLIC


@pytest.mark.parametrize(
    "args",
    [
        ["tool", "set_gaia_connection", "--json", INVALID_JSON[0]],
        ["tool", "set_gaia_connection", "--json", INVALID_JSON[2]],
        ["set-gaia-connection", "--api-key", SECRET, "--" + SECRET],
    ],
)
def test_console_entry_point_stderr_never_contains_secret(args: list[str]):
    process = subprocess.run(
        [sys.executable, "-m", "narumi_server.cli_tools", "--in-process", *args],
        env=dict(os.environ),
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert process.returncode == 2
    assert process.stdout == ""
    assert SECRET not in process.stderr
    assert json.loads(process.stderr)["error"]["code"] == "invalid_argument"
