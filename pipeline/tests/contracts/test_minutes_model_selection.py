"""Explicit text minutes selection contracts and legacy Codex model compatibility."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest
from jsonschema import Draft202012Validator, ValidationError
from narumi.contracts import ContractSet, load_contracts
from narumi.errors import InvalidArgumentError
from narumi.models import MeetingConfig
from pydantic import ValidationError as ModelValidationError

MEETING_ID = "20260827T030500Z-a1b2c3d4"
REQUEST_ID = "provider-contract-request-001"
MINUTES_MODEL = {
    "provider": "codex-app-server",
    "connection_id": "conn-0123456789ab",
    "connection_revision": 1,
    "model_id": "fixture-text-model",
}
PROVIDER_POLICIES = {
    "codex-app-server": "subscription_ok",
    "openai-api": "api_ok",
    "anthropic-api": "api_ok",
    "ollama": "local_only",
}
TOKEN_PROVIDERS = ["openai-api", "anthropic-api", "ollama"]
SELECTIONS = [
    None,
    MINUTES_MODEL,
    {**MINUTES_MODEL, "parameters": {"reasoning_effort": "high"}},
    *[{**MINUTES_MODEL, "provider": provider} for provider in TOKEN_PROVIDERS],
    *[
        {**MINUTES_MODEL, "provider": provider, "parameters": {"max_tokens": 4096}}
        for provider in TOKEN_PROVIDERS
    ],
    {
        **MINUTES_MODEL,
        "provider": "openai-api",
        "parameters": {"reasoning_effort": "low", "max_tokens": 4096},
    },
]


@pytest.fixture(scope="module")
def contracts() -> ContractSet:
    return load_contracts()


@pytest.mark.parametrize("selection", SELECTIONS)
def test_minutes_model_is_shared_across_meeting_and_profile_tools(
    contracts: ContractSet, selection: dict[str, Any] | None
) -> None:
    policy = "local_only" if selection is None else PROVIDER_POLICIES[selection["provider"]]
    for tool in ("set_meeting_config", "set_profile", "start_recording", "import_recording"):
        args = dict(contracts[tool].input_examples[0])
        if tool == "set_meeting_config":
            args.update(minutes_model=selection, external_send_policy=policy)
        else:
            args["config"] = {"minutes_model": selection, "external_send_policy": policy}
        contracts.validate_input(tool, args)
    for tool in (
        "get_meeting",
        "set_meeting_config",
        "get_profile",
        "set_profile",
        "list_profiles",
    ):
        payload = deepcopy(contracts[tool].output_examples[0])
        if tool == "list_profiles":
            config = payload["profiles"][0]["config"]
        elif "profile" in payload:
            config = payload["profile"]["config"]
        else:
            config = payload["config"]
        config.update(minutes_model=selection, external_send_policy=policy)
        contracts.validate_output(tool, payload)


def test_minutes_model_defaults_preserve_legacy_provider_and_roundtrip(
    contracts: ContractSet,
) -> None:
    assert MeetingConfig().minutes_model is None
    assert MeetingConfig(minutes_model=None).minutes_model is None
    config = MeetingConfig.model_validate(
        {"llm_provider": "ollama", "minutes_model": MINUTES_MODEL}
    )
    assert config.llm_provider == "ollama"
    assert config.minutes_model is not None
    assert config.minutes_model.model_dump() == {
        **MINUTES_MODEL,
        "parameters": {},
        "cache_epoch": 0,
    }
    Draft202012Validator(contracts.schema_for_def("meeting_config")).validate(config.model_dump())
    with_parameters = {
        **MINUTES_MODEL,
        "parameters": {"reasoning_effort": "high"},
        "cache_epoch": 2,
    }
    config = MeetingConfig.model_validate({"minutes_model": with_parameters})
    assert config.minutes_model is not None
    assert config.minutes_model.model_dump() == with_parameters


@pytest.mark.parametrize(
    "changes",
    [
        {"provider": "claude-agent-sdk"},
        {"provider": "unknown-provider"},
        {"provider": "none"},
        {"connection_id": "../other"},
        {"connection_revision": 0},
        {"connection_revision": True},
        {"connection_revision": "1"},
        {"model_id": ""},
        {"model_id": "x" * 257},
        {"model_id": None},
        {"cache_epoch": -1},
        {"cache_epoch": True},
        {"cache_epoch": "0"},
        {"parameters": None},
        {"parameters": {"reasoning_effort": None}},
        {"parameters": {"reasoning_effort": ""}},
        {"parameters": {"reasoning_effort": 1}},
        {"parameters": {"reasoning_effort": "high\ncommand"}},
        {"parameters": {"endpoint": "https://unapproved.example"}},
        {"parameters": {"api_key": "example-not-a-real-key"}},
        {"parameters": {"path": "/tmp/runtime"}},
        {"parameters": {"max_tokens": 10}},
        {"api_key": "example-not-a-real-key"},
        {"endpoint": "https://unapproved.example"},
        {"command": "installer"},
    ],
)
def test_minutes_model_rejects_unsupported_or_secret_configuration(
    contracts: ContractSet, changes: dict[str, Any]
) -> None:
    selection = {**MINUTES_MODEL, **changes}
    with pytest.raises(InvalidArgumentError):
        contracts.validate_input(
            "set_meeting_config",
            {"meeting_id": MEETING_ID, "request_id": REQUEST_ID, "minutes_model": selection},
        )
    with pytest.raises(ModelValidationError):
        MeetingConfig.model_validate({"minutes_model": selection})


@pytest.mark.parametrize("field", list(MINUTES_MODEL))
def test_minutes_model_requires_explicit_provider_connection_revision_and_model(
    contracts: ContractSet, field: str
) -> None:
    selection = {key: value for key, value in MINUTES_MODEL.items() if key != field}
    with pytest.raises(InvalidArgumentError):
        contracts.validate_input(
            "set_meeting_config",
            {"meeting_id": MEETING_ID, "request_id": REQUEST_ID, "minutes_model": selection},
        )
    with pytest.raises(ModelValidationError):
        MeetingConfig.model_validate({"minutes_model": selection})


@pytest.mark.parametrize("provider", TOKEN_PROVIDERS)
@pytest.mark.parametrize("max_tokens", [1, 4096, 32768])
def test_token_limited_minutes_accept_application_request_bounds(
    contracts: ContractSet, provider: str, max_tokens: int
) -> None:
    contracts.validate_input(
        "set_meeting_config",
        {
            "meeting_id": MEETING_ID,
            "request_id": REQUEST_ID,
            "external_send_policy": PROVIDER_POLICIES[provider],
            "minutes_model": {
                **MINUTES_MODEL,
                "provider": provider,
                "parameters": {"max_tokens": max_tokens},
            },
        },
    )


@pytest.mark.parametrize("provider", TOKEN_PROVIDERS)
@pytest.mark.parametrize("max_tokens", [0, -1, 32769, True, False, None, "4096", 1.5, [], {}])
def test_token_limited_minutes_reject_invalid_application_request_bounds(
    contracts: ContractSet, provider: str, max_tokens: Any
) -> None:
    with pytest.raises(InvalidArgumentError):
        contracts.validate_input(
            "set_meeting_config",
            {
                "meeting_id": MEETING_ID,
                "request_id": REQUEST_ID,
                "minutes_model": {
                    **MINUTES_MODEL,
                    "provider": provider,
                    "parameters": {"max_tokens": max_tokens},
                },
            },
        )


@pytest.mark.parametrize(
    ("provider", "parameters"),
    [
        ("codex-app-server", {"max_tokens": 4096}),
        ("anthropic-api", {"reasoning_effort": "high"}),
        ("ollama", {"reasoning_effort": "high"}),
        ("openai-api", {"reasoning_effort": None}),
        ("openai-api", {"reasoning_effort": ""}),
        ("openai-api", {"reasoning_effort": "x" * 33}),
        ("openai-api", {"reasoning_effort": True}),
        ("openai-api", {"reasoning_effort": "high\ncommand"}),
    ],
)
def test_minutes_parameters_are_specific_to_the_selected_provider(
    contracts: ContractSet, provider: str, parameters: dict[str, Any]
) -> None:
    with pytest.raises(InvalidArgumentError):
        contracts.validate_input(
            "set_meeting_config",
            {
                "meeting_id": MEETING_ID,
                "request_id": REQUEST_ID,
                "minutes_model": {**MINUTES_MODEL, "provider": provider, "parameters": parameters},
            },
        )


@pytest.mark.parametrize("provider", list(PROVIDER_POLICIES))
@pytest.mark.parametrize("key", ["api_key", "token", "endpoint", "command", "path", "temperature"])
def test_each_minutes_provider_has_a_closed_parameter_schema(
    contracts: ContractSet, provider: str, key: str
) -> None:
    with pytest.raises(InvalidArgumentError):
        contracts.validate_input(
            "set_meeting_config",
            {
                "meeting_id": MEETING_ID,
                "request_id": REQUEST_ID,
                "minutes_model": {
                    **MINUTES_MODEL,
                    "provider": provider,
                    "parameters": {key: "example-not-a-real-secret"},
                },
            },
        )


@pytest.mark.parametrize("model_limit", [None, 1024, 8192])
def test_application_token_limits_do_not_fabricate_model_capability(
    contracts: ContractSet, model_limit: int | None
) -> None:
    payload = deepcopy(contracts["list_provider_models"].output_examples[0])
    model = payload["models"][0]
    request_maximum = 32768 if model_limit is None else min(32768, model_limit)
    default = 4096 if model_limit is None else min(4096, model_limit)
    model.update(
        availability="available",
        reason=None,
        source="local_catalog",
        max_output_tokens=model_limit,
    )
    model["billing"]["kind"] = "local"
    model["parameter_schema"] = {
        "type": "object",
        "properties": {
            "max_tokens": {
                "type": "integer",
                "minimum": 1,
                "maximum": request_maximum,
                "default": default,
            }
        },
        "required": [],
        "additionalProperties": False,
    }
    contracts.validate_output("list_provider_models", payload)
    parameter_validator = Draft202012Validator(model["parameter_schema"])
    parameter_validator.validate({"max_tokens": default})
    with pytest.raises(ValidationError):
        parameter_validator.validate({"max_tokens": request_maximum + 1})
    assert model["max_output_tokens"] == model_limit


@pytest.mark.parametrize("tool", ["regenerate", "register_context"])
@pytest.mark.parametrize("selection", SELECTIONS)
def test_config_snapshots_share_all_supported_minutes_selection_schemas(
    contracts: ContractSet, tool: str, selection: dict[str, Any] | None
) -> None:
    args = deepcopy(contracts[tool].input_examples[0])
    policy = "local_only" if selection is None else PROVIDER_POLICIES[selection["provider"]]
    args["expected_config"] = {"minutes_model": selection, "external_send_policy": policy}
    contracts.validate_input(tool, args)


@pytest.mark.parametrize("tool", ["regenerate", "register_context"])
def test_generation_entry_points_accept_confirmed_full_config(
    contracts: ContractSet, tool: str
) -> None:
    args = deepcopy(contracts[tool].input_examples[0])
    if tool == "register_context":
        args["auto_regenerate"] = True
    config = MeetingConfig.model_validate(
        {"minutes_model": MINUTES_MODEL, "external_send_policy": "subscription_ok"}
    ).model_dump()
    contracts.validate_input(tool, {**args, "expected_config": config})
    config["minutes_model"] = None
    contracts.validate_input(tool, {**args, "expected_config": config})
    # Whether omission is safe depends on saved state, which the server checks under its lock.
    contracts.validate_input(tool, args)
    if tool == "register_context":
        args["auto_regenerate"] = False
        contracts.validate_input(tool, args)
        contracts.validate_input(tool, {**args, "expected_config": config})


@pytest.mark.parametrize("tool", ["regenerate", "register_context"])
@pytest.mark.parametrize(
    "expected_config",
    [
        None,
        "current",
        [],
        {"external_send_policy": "unrestricted"},
        {"api_key": "example-not-a-real-key"},
        {"minutes_model": {**MINUTES_MODEL, "parameters": {"path": "/tmp/runtime"}}},
        {"minutes_model": {**MINUTES_MODEL, "connection_revision": 0}},
        {"minutes_model": {**MINUTES_MODEL, "model_id": None}},
    ],
)
def test_generation_entry_points_reject_invalid_config_snapshots(
    contracts: ContractSet, tool: str, expected_config: Any
) -> None:
    args = contracts[tool].input_examples[0]
    with pytest.raises(InvalidArgumentError):
        contracts.validate_input(tool, {**args, "expected_config": expected_config})
