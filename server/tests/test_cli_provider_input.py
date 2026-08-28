"""Provider CLI input stays out of argv, diagnostics and unauthorized local execution."""

from __future__ import annotations

import getpass
import json
import sys
from dataclasses import replace
from typing import Any

import click
import pytest
from click.testing import CliRunner
from narumi.contracts import ContractSet, load_contracts
from narumi.errors import InvalidArgumentError
from narumi_server import cli_tools
from narumi_server.cli_input import (
    MAX_STDIN_CHARACTERS,
    SecretStdinOption,
    build_tool_input,
    collect_args,
)

SECRET = "fake-provider-cli-key-743019"
CREATE = [
    "set-provider-connection",
    "--provider-id",
    "anthropic-api",
    "--display-name",
    "Meeting provider",
    "--auth-method",
    "api_key",
]


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def call(state, tool, arguments):
        assert state.confidential
        calls.append({"tool": tool, "arguments": arguments})
        return {"credential_present": "api_key" in arguments}, False

    monkeypatch.setattr(cli_tools, "_call", call)
    return calls


def test_secret_option_has_no_argv_value_and_explains_input_methods():
    inputs = build_tool_input(load_contracts()["set_provider_connection"])
    prompt = next(option for option in inputs.options if option.name == "api_key")
    assert prompt.is_flag
    assert inputs.secret_options["api_key"].opts == ["--api-key-stdin"]
    assert inputs.null_options["api_key"].opts == ["--clear-api-key"]
    result = CliRunner().invoke(cli_tools.build_cli(), ["set-provider-connection", "--help"])
    assert result.exit_code == 0
    assert "--api-key TEXT" not in result.output
    assert "Prompt without echo" in result.output
    assert "--api-key-stdin" in result.output
    assert "--clear-api-key" in result.output


def test_hidden_prompt_does_not_echo_or_accept_an_argv_secret(captured, monkeypatch):
    seen: list[dict[str, Any]] = []

    def prompt(_label, **kwargs):
        seen.append(kwargs)
        return SECRET

    monkeypatch.setattr(click, "prompt", prompt)
    cli = cli_tools.build_cli()
    result = CliRunner().invoke(cli, [*CREATE, "--api-key"], catch_exceptions=False)
    assert result.exit_code == 0
    assert seen == [{"hide_input": True, "err": True, "type": str}]
    assert captured[0]["arguments"]["api_key"] == SECRET
    rejected = CliRunner().invoke(cli, [*CREATE, "--api-key", SECRET], catch_exceptions=False)
    assert rejected.exit_code == 2
    assert len(captured) == 1
    assert SECRET not in result.output + rejected.output


def test_getpass_echo_fallback_is_rejected_before_reading_any_secret(monkeypatch, capsys):
    class UnreadableInput:
        def readline(self, *_args):
            pytest.fail("getpass must not read after failing to disable terminal echo")

    monkeypatch.setattr(sys, "stdin", UnreadableInput())
    monkeypatch.setattr(click.termui, "hidden_prompt_func", getpass.fallback_getpass)
    contract = load_contracts()["set_provider_connection"]
    with pytest.raises(InvalidArgumentError, match="echo could not be disabled"):
        collect_args(contract, build_tool_input(contract), {"api_key": True})
    captured_output = capsys.readouterr()
    assert SECRET not in captured_output.out + captured_output.err


@pytest.mark.parametrize("value", [SECRET, "null", " spaced-value "])
def test_stdin_preserves_string_and_omission_and_clear_are_distinct(captured, value):
    cli = cli_tools.build_cli()
    runner = CliRunner()
    result = runner.invoke(cli, [*CREATE, "--api-key-stdin"], input=value + "\n")
    assert result.exit_code == 0
    assert captured[-1]["arguments"]["api_key"] == value
    assert value not in result.output
    assert runner.invoke(cli, CREATE).exit_code == 0
    assert "api_key" not in captured[-1]["arguments"]
    assert runner.invoke(cli, [*CREATE, "--clear-api-key"]).exit_code == 0
    assert captured[-1]["arguments"]["api_key"] is None


@pytest.mark.parametrize(
    "flags",
    [
        ["--api-key", "--api-key-stdin"],
        ["--clear-api-key", "--api-key-stdin"],
        ["--api-key", "--clear-api-key"],
    ],
)
def test_secret_input_methods_conflict_before_prompt_or_submission(captured, monkeypatch, flags):
    monkeypatch.setattr(click, "prompt", lambda *_a, **_k: pytest.fail("must not prompt"))
    result = CliRunner().invoke(cli_tools.build_cli(), [*CREATE, *flags], input=SECRET)
    assert result.exit_code == 2
    assert not captured
    assert SECRET not in result.output


@pytest.mark.parametrize("contents", ["", "x" * (MAX_STDIN_CHARACTERS + 1)])
def test_empty_or_oversized_secret_stdin_is_rejected(captured, contents):
    result = CliRunner().invoke(cli_tools.build_cli(), [*CREATE, "--api-key-stdin"], input=contents)
    assert result.exit_code == 2
    assert json.loads(result.stderr)["error"]["code"] == "invalid_argument"
    assert not captured


def test_json_literal_rejects_secret_but_stdin_accepts_it(captured):
    cli = cli_tools.build_cli()
    document = json.dumps({"api_key": SECRET})
    rejected = CliRunner().invoke(cli, ["tool", "set_provider_connection", "--json", document])
    assert rejected.exit_code == 2
    assert not captured
    accepted = CliRunner().invoke(
        cli, ["tool", "set_provider_connection", "--json-stdin"], input=document
    )
    assert accepted.exit_code == 0
    assert captured[0]["arguments"]["api_key"] == SECRET
    assert SECRET not in rejected.output + accepted.output
    assert (
        CliRunner()
        .invoke(cli, ["tool", "set_provider_connection", "--json", '{"api_key":null}'])
        .exit_code
        == 0
    )
    assert captured[-1]["arguments"]["api_key"] is None


def test_stdin_json_errors_and_conflicting_sources_never_echo_values(captured):
    cli = cli_tools.build_cli()
    for options in (["--json-stdin"], ["--json", "{}", "--json-stdin"]):
        result = CliRunner().invoke(
            cli, ["tool", "set_provider_connection", *options], input='{"api_key":"' + SECRET
        )
        assert result.exit_code == 2
        assert SECRET not in result.output
    assert not captured


def test_write_only_refs_and_nested_containers_get_stdin_controls(captured):
    contracts = load_contracts()
    probe = replace(
        contracts["set_provider_connection"],
        name="secret_probe",
        input_schema={
            "type": "object",
            "properties": {
                "credentials": {"$ref": "#/$defs/credentials"},
                "credentials_stdin": {"type": "string"},
            },
            "$defs": {
                "credentials": {
                    "type": "object",
                    "properties": {"key": {"type": "string", "writeOnly": True}},
                }
            },
        },
    )
    subset = ContractSet(
        name=contracts.name,
        contract_version=contracts.contract_version,
        tools={probe.name: probe},
        defs=contracts.defs,
    )
    cli = cli_tools.build_cli(subset)
    inputs = build_tool_input(probe)
    assert isinstance(inputs.secret_options["credentials"], SecretStdinOption)
    assert inputs.secret_options["credentials"].opts == ["--credentials-stdin-value"]
    document = json.dumps({"key": SECRET})
    result = CliRunner().invoke(cli, ["secret-probe", "--credentials-stdin-value"], input=document)
    assert result.exit_code == 0
    assert captured[-1]["arguments"]["credentials"] == {"key": SECRET}
    bad = CliRunner().invoke(
        cli, ["tool", "secret_probe", "--json", json.dumps({"credentials": {"key": SECRET}})]
    )
    assert bad.exit_code == 2
    assert SECRET not in bad.output


@pytest.mark.parametrize("tool", sorted(cli_tools.PROVIDER_TOOLS | {"set_gaia_connection"}))
def test_providers_and_write_only_tools_cannot_run_in_process(home, monkeypatch, tool):
    def forbidden(*_args, **_kwargs):
        pytest.fail("in-process provider calls must not build context or read credentials")

    monkeypatch.setattr(cli_tools, "build_context", forbidden)
    monkeypatch.setattr(cli_tools, "load_client_transport", forbidden)
    result = CliRunner().invoke(
        cli_tools.build_cli(), ["--in-process", "tool", tool, "--json-stdin"], input="{}"
    )
    assert result.exit_code == 2
    assert json.loads(result.stderr)["error"]["code"] == "authentication_required"
    assert not (home / "gaia.json").exists()


@pytest.mark.parametrize("error", [False, True])
def test_even_a_successful_response_cannot_echo_the_submitted_secret(monkeypatch, error):
    monkeypatch.setattr(
        cli_tools, "_call", lambda *_args: ({"connection": {"display_name": SECRET}}, error)
    )
    result = CliRunner().invoke(cli_tools.build_cli(), [*CREATE, "--api-key-stdin"], input=SECRET)
    assert result.exit_code == 2
    assert SECRET not in result.output
    assert json.loads(result.stderr)["error"]["code"] == "internal"
