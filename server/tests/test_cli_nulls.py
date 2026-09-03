"""Nullable contract inputs have explicit, non-conflicting CLI controls for JSON null."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import click
import pytest
from click.testing import CliRunner
from narumi.contracts import ContractSet, load_contracts
from narumi.gaia.settings import GaiaConnectionStore
from narumi_server import cli_tools
from narumi_server.cli_input import NullOption, SecretStdinOption, build_tool_input

URL = "http://127.0.0.1:4111/mcp"
SECRET = "fake-cli-null-secret-90674"


@pytest.fixture(autouse=True)
def isolate_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("NARUMI_HOME", str(tmp_path / "data"))
    for name in ("NARUMI_GAIA_URL", "NARUMI_GAIA_API_KEY", "NARUMI_CONTRACTS_DIR"):
        monkeypatch.delenv(name, raising=False)


def invoke(cli: click.Group, args: list[str], *, input: str | None = None, local: bool = True):
    prefix = ["--in-process"] if local else []
    return CliRunner().invoke(cli, [*prefix, *args], input=input, catch_exceptions=False)


def probe_cli(
    monkeypatch: pytest.MonkeyPatch,
    properties: dict[str, Any],
    *,
    required: tuple[str, ...] = (),
    defs: dict[str, Any] | None = None,
) -> tuple[click.Group, dict[str, Any]]:
    source = load_contracts()
    contract = replace(
        source["set_gaia_connection"],
        name="nullable_probe",
        input_schema={
            "type": "object",
            "properties": properties,
            "required": list(required),
            "additionalProperties": False,
            "$defs": defs or {},
        },
    )
    contracts = ContractSet(
        name=source.name,
        contract_version=source.contract_version,
        tools={contract.name: contract},
        defs=source.defs,
    )
    seen: dict[str, Any] = {}

    def call(_state, tool, arguments):
        contracts.validate_input(tool, arguments)
        seen["arguments"] = arguments
        return {"ok": True}, False

    monkeypatch.setattr(cli_tools, "_call", call)
    return cli_tools.build_cli(contracts), seen


def test_real_contracts_get_clear_flags_only_for_nullable_top_level_properties():
    expected = {
        "set_gaia_connection": {"url", "api_key"},
        "set_profile": {"scope", "engagement"},
        "set_meeting_config": {"self_name", "new_scope", "minutes_model", "transcription_model"},
        "set_provider_connection": {"api_key", "chat_max_tokens_field"},
        "list_provider_models": {"cursor"},
    }
    for contract in load_contracts():
        inputs = build_tool_input(contract)
        assert set(inputs.null_options) == expected.get(contract.name, set())
        primary = {
            option.name
            for option in inputs.options
            if not isinstance(option, (NullOption, SecretStdinOption))
        }
        assert primary == set(contract.input_schema["properties"])
        flags = [
            flag for option in inputs.options for flag in (*option.opts, *option.secondary_opts)
        ]
        assert len(flags) == len(set(flags))


@pytest.mark.parametrize(
    "schema",
    [
        {"type": ["string", "null"]},
        {"type": ["integer", "null"]},
        {"type": ["number", "null"]},
        {"type": ["boolean", "null"]},
        {"type": ["array", "null"], "items": {"type": "string"}},
        {"type": ["object", "null"]},
        {"type": "null"},
        {"oneOf": [{"type": "string"}, {"type": "null"}]},
        {"anyOf": [{"type": "array"}, {"type": "null"}]},
        {"allOf": [{"type": ["string", "null"]}, {"maxLength": 20}]},
        {"enum": ["retained", None]},
        {"const": None},
        {"$ref": "#/$defs/nullable_text"},
        True,
    ],
)
def test_clear_supports_nullable_types_refs_and_composition(
    monkeypatch: pytest.MonkeyPatch, schema: Any
):
    cli, seen = probe_cli(
        monkeypatch,
        {"value": schema},
        defs={"nullable_text": {"type": ["string", "null"]}},
    )
    result = invoke(cli, ["nullable-probe", "--clear-value"])
    assert result.exit_code == 0, result.stderr
    assert seen["arguments"] == {"value": None}


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "string"},
        {"type": ["string", "null"], "not": {"type": "null"}},
        {"type": ["string", "null"], "enum": ["retained"]},
        {"allOf": [{"type": ["string", "null"]}, {"type": "string"}]},
        {"$ref": "#/$defs/nullable_text", "type": "string"},
        {"oneOf": [{"type": "null"}, {"type": "null"}]},
        False,
    ],
)
def test_nonnullable_or_restricted_null_schema_has_no_clear_flag(
    monkeypatch: pytest.MonkeyPatch, schema: Any
):
    cli, seen = probe_cli(
        monkeypatch,
        {"value": schema},
        defs={"nullable_text": {"type": ["string", "null"]}},
    )
    result = invoke(cli, ["nullable-probe", "--clear-value"])
    assert result.exit_code == 2
    assert not seen
    command = cli.commands["nullable-probe"]
    assert not any(isinstance(option, NullOption) for option in command.params)


def test_omission_is_not_null_and_nullable_boolean_false_is_not_omission(
    monkeypatch: pytest.MonkeyPatch,
):
    cli, seen = probe_cli(monkeypatch, {"enabled": {"type": ["boolean", "null"]}})
    assert invoke(cli, ["nullable-probe"]).exit_code == 0
    assert seen["arguments"] == {}
    assert invoke(cli, ["nullable-probe", "--no-enabled"]).exit_code == 0
    assert seen["arguments"] == {"enabled": False}
    assert invoke(cli, ["nullable-probe", "--enabled"]).exit_code == 0
    assert seen["arguments"] == {"enabled": True}
    assert invoke(cli, ["nullable-probe", "--clear-enabled"]).exit_code == 0
    assert seen["arguments"] == {"enabled": None}


def test_required_nullable_property_accepts_value_or_clear_but_not_omission(
    monkeypatch: pytest.MonkeyPatch,
):
    cli, seen = probe_cli(monkeypatch, {"value": {"type": ["string", "null"]}}, required=("value",))
    assert invoke(cli, ["nullable-probe"]).exit_code == 2
    assert not seen
    assert invoke(cli, ["nullable-probe", "--clear-value"]).exit_code == 0
    assert seen["arguments"] == {"value": None}
    assert invoke(cli, ["nullable-probe", "--value", "kept"]).exit_code == 0
    assert seen["arguments"] == {"value": "kept"}
    assert "required: value or --clear-value" in invoke(cli, ["nullable-probe", "--help"]).stdout


@pytest.mark.parametrize(
    ("schema", "value_args"),
    [
        ({"type": ["string", "null"]}, ["--value", "kept"]),
        ({"type": ["integer", "null"]}, ["--value", "0"]),
        ({"type": ["number", "null"]}, ["--value", "0.0"]),
        ({"type": ["boolean", "null"]}, ["--no-value"]),
        ({"type": ["object", "null"]}, ["--value", "{}"]),
        ({"type": ["array", "null"]}, ["--value", "[]"]),
    ],
)
@pytest.mark.parametrize("clear_first", [False, True])
def test_value_and_clear_are_exclusive_in_both_orders(
    monkeypatch: pytest.MonkeyPatch,
    schema: dict[str, Any],
    value_args: list[str],
    clear_first: bool,
):
    cli, seen = probe_cli(monkeypatch, {"value": schema})
    args = ["--clear-value", *value_args] if clear_first else [*value_args, "--clear-value"]
    result = invoke(cli, ["nullable-probe", *args])
    assert result.exit_code == 2
    assert json.loads(result.stderr)["error"]["code"] == "invalid_argument"
    assert not seen


@pytest.mark.parametrize(
    "schema",
    [
        {"type": ["string", "null"]},
        {"oneOf": [{"type": "string"}, {"type": "null"}]},
        {"type": ["string", "array", "null"]},
        {"allOf": [{"type": ["string", "null"]}, {"maxLength": 20}]},
        {"enum": [None, "null"]},
    ],
)
def test_literal_null_is_never_changed_into_a_null_value(
    monkeypatch: pytest.MonkeyPatch, schema: dict[str, Any]
):
    cli, seen = probe_cli(monkeypatch, {"value": schema})
    result = invoke(cli, ["nullable-probe", "--value", "null"])
    assert result.exit_code == 0, result.stderr
    assert seen["arguments"] == {"value": "null"}


def test_clear_flag_and_internal_name_collisions_do_not_shadow_contract_properties(
    monkeypatch: pytest.MonkeyPatch,
):
    properties = {
        "url": {"type": ["string", "null"]},
        "clear_url": {"type": ["string", "null"]},
        "clear_url_value": {"type": "string"},
        "clear_url_value_2": {"type": "boolean"},
        "_narumi_clear_0": {"type": "string"},
    }
    cli, seen = probe_cli(monkeypatch, properties)
    result = invoke(
        cli,
        [
            "nullable-probe",
            "--clear-url-value-3",
            "--clear-url",
            "literal field value",
            "--clear-url-value",
            "also retained",
            "--clear-url-value-2",
            "---narumi-clear-0",
            "original property",
        ],
    )
    assert result.exit_code == 0, result.stderr
    assert seen["arguments"] == {
        "url": None,
        "clear_url": "literal field value",
        "clear_url_value": "also retained",
        "clear_url_value_2": True,
        "_narumi_clear_0": "original property",
    }
    command = cli.commands["nullable-probe"]
    names = [option.name for option in command.params]
    assert len(names) == len(set(names))
    flags = [flag for option in command.params for flag in (*option.opts, *option.secondary_opts)]
    assert len(flags) == len(set(flags))
    assert "--clear-url-value-3" in invoke(cli, ["nullable-probe", "--help"]).stdout
    assert invoke(cli, ["nullable-probe", "--clear-clear-url"]).exit_code == 0
    assert seen["arguments"] == {"clear_url": None}


@pytest.fixture
def gaia_call(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> GaiaConnectionStore:
    """Exercise nullable argument forwarding after replacing only the resident call boundary."""
    store = GaiaConnectionStore(tmp_path / "data" / "gaia.json", environ={})
    contracts = load_contracts()

    def call(state, tool, arguments):
        assert state.mode != cli_tools.MODE_IN_PROCESS
        contracts.validate_input(tool, arguments)
        store.set(**{key: value for key, value in arguments.items() if key != "request_id"})
        return {"connection": store.get()}, False

    monkeypatch.setattr(cli_tools, "_call", call)
    return store


def test_gaia_clear_key_then_clear_url_through_regular_subcommand(gaia_call: GaiaConnectionStore):
    cli = cli_tools.build_cli()
    store = gaia_call
    store.set(url=URL, api_key=SECRET)
    clear_key = invoke(cli, ["set-gaia-connection", "--clear-api-key"], local=False)
    assert clear_key.exit_code == 0, clear_key.stderr
    assert store.get() == {"url": URL, "has_api_key": False, "source": "saved"}
    store.set(api_key=SECRET)
    clear_url = invoke(cli, ["set-gaia-connection", "--clear-url"], local=False)
    assert clear_url.exit_code == 0, clear_url.stderr
    assert store.get() == {"url": None, "has_api_key": False, "source": "saved"}
    assert json.loads(store.path.read_text())["api_key"] is None
    assert SECRET not in clear_key.output + clear_url.output


@pytest.mark.parametrize(
    "args",
    [
        ["--api-key", SECRET, "--clear-api-key"],
        ["--clear-api-key", "--api-key", SECRET],
        ["--url", f"http://127.0.0.1/{SECRET}", "--clear-url"],
    ],
)
def test_conflicting_secret_clear_requests_fail_without_leak_or_mutation(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, args: list[str]
):
    store = GaiaConnectionStore(tmp_path / "data" / "gaia.json", environ={})
    store.set(url=URL, api_key=SECRET)
    previous = store.path.read_bytes()
    result = invoke(cli_tools.build_cli(), ["set-gaia-connection", *args])
    assert result.exit_code == 2
    assert SECRET not in result.output
    assert SECRET not in caplog.text
    assert store.path.read_bytes() == previous


def test_gaia_literal_null_key_is_a_key_and_literal_null_url_does_not_disable(
    gaia_call: GaiaConnectionStore,
):
    store = gaia_call
    store.set(url=URL, api_key=SECRET)
    cli = cli_tools.build_cli()
    assert (
        invoke(
            cli, ["set-gaia-connection", "--api-key-stdin"], input="null\n", local=False
        ).exit_code
        == 0
    )
    assert json.loads(store.path.read_text())["api_key"] == "null"
    assert store.get()["has_api_key"] is True
    assert invoke(cli, ["set-gaia-connection", "--url", "null"], local=False).exit_code == 2
    assert store.get()["url"] == URL


def test_console_clear_flag_cannot_mutate_credentials_in_process(tmp_path: Path):
    store = GaiaConnectionStore(tmp_path / "data" / "gaia.json", environ={})
    store.set(url=URL, api_key=SECRET)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "narumi_server.cli_tools",
            "--in-process",
            "set-gaia-connection",
            "--clear-url",
        ],
        env=dict(os.environ),
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 2
    assert SECRET not in result.stdout + result.stderr
    assert json.loads(result.stderr)["error"]["code"] == "authentication_required"
    assert store.get() == {"url": URL, "has_api_key": True, "source": "saved"}
