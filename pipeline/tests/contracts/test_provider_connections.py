"""Provider setup contracts: closed inputs, versioned mutations and safe public metadata."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest
from jsonschema import Draft202012Validator, ValidationError
from narumi.contracts import ContractSet, load_contracts
from narumi.errors import ContractMismatchError, InvalidArgumentError
from narumi.models import MeetingConfig
from pydantic import ValidationError as ModelValidationError

CONNECTION_ID = "conn-0123456789ab"
OPERATION_ID = "auth-0123456789ab"
REQUEST_ID = "provider-contract-request-001"
MEETING_ID = "20260827T030500Z-a1b2c3d4"
MINUTES_MODEL = {
    "provider": "codex-app-server",
    "connection_id": "conn-0123456789ab",
    "connection_revision": 1,
    "model_id": "fixture-text-model",
}
AUTHORIZATION_URL = (
    "https://auth.openai.com/oauth/authorize?response_type=code&client_id=contract-client"
    "&redirect_uri=http%3A%2F%2Flocalhost%3A1455%2Fauth%2Fcallback&state=contract-state"
    "&code_challenge=contract-challenge&code_challenge_method=S256"
)
CREATE = {
    "provider_id": "anthropic-api",
    "display_name": "議事録 API",
    "auth_method": "api_key",
    "request_id": REQUEST_ID,
}
UPDATE = {
    "connection_id": CONNECTION_ID,
    "expected_revision": 1,
    "request_id": REQUEST_ID,
}


@pytest.fixture(scope="module")
def contracts() -> ContractSet:
    return load_contracts()


@pytest.mark.parametrize(
    "provider", ["anthropic-api", "claude-agent-sdk", "codex-app-server", "ollama"]
)
def test_new_connection_can_be_saved_before_authentication(
    contracts: ContractSet, provider: str
) -> None:
    args = {**CREATE, "provider_id": provider}
    if provider == "ollama":
        args.update(auth_method="none", endpoint="http://127.0.0.1:11434")
    elif provider == "codex-app-server":
        args.update(auth_method="chatgpt", endpoint="https://chatgpt.com")
    contracts.validate_input("set_provider_connection", args)


@pytest.mark.parametrize(
    "changes",
    [
        {"auth_method": "api_key"},
        {"auth_method": "none"},
        {"api_key": "example-not-a-real-key"},
        {"api_key": None},
        {"endpoint": "https://api.openai.com"},
        {"endpoint": "https://chatgpt.com/"},
        {"endpoint": "https://chatgpt.com/?token=secret"},
        {"endpoint": "https://chatgpt.com.evil.example"},
        {"codex_home": "/tmp/user-controlled"},
    ],
)
def test_codex_connection_rejects_api_keys_and_runtime_overrides(
    contracts: ContractSet, changes: dict[str, Any]
) -> None:
    with pytest.raises(InvalidArgumentError):
        contracts.validate_input(
            "set_provider_connection",
            {**CREATE, "provider_id": "codex-app-server", "auth_method": "chatgpt", **changes},
        )


@pytest.mark.parametrize("api_key", [None, "example-not-a-real-key"])
def test_chatgpt_auth_updates_do_not_accept_an_api_key(
    contracts: ContractSet, api_key: str | None
) -> None:
    with pytest.raises(InvalidArgumentError):
        contracts.validate_input(
            "set_provider_connection", {**UPDATE, "auth_method": "chatgpt", "api_key": api_key}
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"display_name": "変更した表示名"},
        {"enabled": False},
        {"enabled": True},
        {"api_key": "example-not-a-real-key"},
        {"api_key": None},
    ],
)
def test_connection_update_supports_key_retention_deletion_and_disable(
    contracts: ContractSet, changes: dict[str, Any]
) -> None:
    contracts.validate_input("set_provider_connection", {**UPDATE, **changes})


@pytest.mark.parametrize(
    "changes",
    [
        {"provider_id": "openai-api"},
        {"provider_id": "codex-app-server"},
        {"provider_id": "arbitrary-provider"},
        {"provider_id": "ollama"},
        {"auth_method": "claude_subscription"},
        {"auth_method": "chatgpt"},
        {"auth_method": "none"},
        {"endpoint": "https://unapproved.example"},
        {"endpoint": "https://api.anthropic.com/redirect"},
        {"display_name": ""},
        {"expected_revision": 1},
        {"connection_id": CONNECTION_ID},
        {"model_id": "implicitly-selected"},
        {"secret_ref": "other-connection"},
        {"command": "installer"},
    ],
)
def test_new_connection_rejects_unimplemented_or_ambiguous_configuration(
    contracts: ContractSet, changes: dict[str, Any]
) -> None:
    with pytest.raises(InvalidArgumentError):
        contracts.validate_input("set_provider_connection", {**CREATE, **changes})


@pytest.mark.parametrize(
    "args",
    [
        UPDATE,
        {**UPDATE, "display_name": "更新", "provider_id": "anthropic-api"},
        {**UPDATE, "enabled": False, "expected_revision": 0},
        {**UPDATE, "enabled": False, "expected_revision": None},
        {**UPDATE, "enabled": False, "expected_revision": "1"},
        {**UPDATE, "enabled": False, "connection_id": "../other-connection"},
        {"connection_id": CONNECTION_ID, "enabled": False, "request_id": REQUEST_ID},
    ],
)
def test_updates_require_an_existing_identity_revision_and_mutation(
    contracts: ContractSet, args: dict[str, Any]
) -> None:
    with pytest.raises(InvalidArgumentError):
        contracts.validate_input("set_provider_connection", args)


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://cloud.ollama.example",
        "http://192.168.1.10:11434",
        "http://localhost:11434",
        "http://127.0.0.256:11434",
        "http://127.000.0.1:11434",
        "http://127.0.0.1:11434/?token=secret",
        "http://localhost:11434#fragment",
        "http://user:secret@127.0.0.1:11434",
    ],
)
def test_local_provider_creation_rejects_non_loopback_or_credential_urls(
    contracts: ContractSet, endpoint: str
) -> None:
    with pytest.raises(InvalidArgumentError):
        contracts.validate_input(
            "set_provider_connection",
            {**CREATE, "provider_id": "ollama", "auth_method": "none", "endpoint": endpoint},
        )


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://127.0.0.1:11434",
        "https://127.22.1.255:443",
        "http://[::1]:11434/",
        "https://[::1]",
    ],
)
def test_local_provider_accepts_numeric_loopback_base_urls(
    contracts: ContractSet, endpoint: str
) -> None:
    contracts.validate_input(
        "set_provider_connection",
        {**CREATE, "provider_id": "ollama", "auth_method": "none", "endpoint": endpoint},
    )


@pytest.mark.parametrize("confirm", [False, None, "true", 1])
def test_provider_deletion_requires_literal_confirmation(
    contracts: ContractSet, confirm: Any
) -> None:
    with pytest.raises(InvalidArgumentError):
        contracts.validate_input("delete_provider_connection", {**UPDATE, "confirm": confirm})


@pytest.mark.parametrize(
    "changes",
    [
        {"resource_id": "../../runtime"},
        {"resource_id": "https://installer.example"},
        {"expected_catalog_revision": ""},
        {"action": "install_global"},
        {"command": "installer"},
        {"download_url": "https://installer.example"},
        {"install_path": "/tmp/unapproved"},
    ],
)
def test_runtime_setup_accepts_only_catalog_identifiers(
    contracts: ContractSet, changes: dict[str, Any]
) -> None:
    base = contracts["prepare_provider_runtime"].input_examples[0]
    with pytest.raises(InvalidArgumentError):
        contracts.validate_input("prepare_provider_runtime", {**base, **changes})


def test_provider_setup_job_does_not_need_a_meeting(contracts: ContractSet) -> None:
    job = {
        "job_id": "job-0123456789ab",
        "kind": "provider_setup",
        "status": "queued",
        "created_at": "2026-08-28T09:00:00Z",
        "updated_at": "2026-08-28T09:00:00Z",
    }
    contracts.validate_output("get_job_status", {"job": job})


@pytest.mark.parametrize(
    "lookup", [{"operation_id": OPERATION_ID}, {"start_request_id": REQUEST_ID}]
)
def test_auth_status_recovers_a_lost_start_response(
    contracts: ContractSet, lookup: dict[str, str]
) -> None:
    contracts.validate_input("get_provider_auth_status", {"connection_id": CONNECTION_ID, **lookup})


@pytest.mark.parametrize(
    "lookup",
    [
        {},
        {"operation_id": OPERATION_ID, "start_request_id": REQUEST_ID},
        {"operation_id": "../another-operation"},
        {"start_request_id": "short"},
        {"request_id": REQUEST_ID},
    ],
)
def test_auth_status_requires_one_unambiguous_lookup(
    contracts: ContractSet, lookup: dict[str, Any]
) -> None:
    with pytest.raises(InvalidArgumentError):
        contracts.validate_input(
            "get_provider_auth_status", {"connection_id": CONNECTION_ID, **lookup}
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"action": "cancel"},
        {"action": "start", "operation_id": OPERATION_ID},
        {"action": "logout", "operation_id": OPERATION_ID},
        {"action": "subscription_login"},
        {"authorization_url": "https://unapproved.example"},
        {"api_key": "example-not-a-real-key"},
    ],
)
def test_authentication_requires_explicit_supported_actions(
    contracts: ContractSet, changes: dict[str, Any]
) -> None:
    with pytest.raises(InvalidArgumentError):
        contracts.validate_input(
            "authenticate_provider_connection", {**UPDATE, "action": "start", **changes}
        )


def test_unknown_auth_operation_is_not_an_idle_success(contracts: ContractSet) -> None:
    payload = deepcopy(contracts["get_provider_auth_status"].output_examples[1])
    contracts.validate_output("get_provider_auth_status", payload)
    payload["operation"]["state"] = "idle"
    with pytest.raises(ContractMismatchError):
        contracts.validate_output("get_provider_auth_status", payload)


@pytest.mark.parametrize("tool", ["authenticate_provider_connection", "get_provider_auth_status"])
def test_only_pending_authentication_start_can_publish_a_login_url(
    contracts: ContractSet, tool: str
) -> None:
    payload = deepcopy(contracts[tool].output_examples[0])
    payload["operation"].update(state="pending", authorization_url=AUTHORIZATION_URL)
    contracts.validate_output(tool, payload)
    for state in ("succeeded", "failed", "cancelled", "unknown"):
        payload["operation"]["state"] = state
        with pytest.raises(ContractMismatchError):
            contracts.validate_output(tool, payload)
        payload["operation"]["authorization_url"] = None
        contracts.validate_output(tool, payload)
        payload["operation"]["authorization_url"] = AUTHORIZATION_URL
    payload["operation"].update(state="pending", action="logout")
    with pytest.raises(ContractMismatchError):
        contracts.validate_output(tool, payload)
    payload["operation"]["action"] = "cancel"
    with pytest.raises(ContractMismatchError):
        contracts.validate_output(tool, payload)


@pytest.mark.parametrize(
    "url",
    [
        "http://auth.openai.com/oauth/authorize?state=fixture",
        "https://chatgpt.com/oauth/authorize?state=fixture",
        "https://auth.openai.com.evil.example/oauth/authorize?state=fixture",
        "https://evil.example@auth.openai.com/oauth/authorize?state=fixture",
        "https://auth.openai.com:1234/oauth/authorize?state=fixture",
        "https://auth.openai.com/oauth/authorize",
        "https://auth.openai.com/oauth/authorize/?state=fixture",
        "https://auth.openai.com/oauth/token?state=fixture",
        "https://auth.openai.com/oauth/authorize?state=fixture#fragment",
        "https://auth.openai.com/oauth/authorize?state=fixture\ncommand",
        "https://auth.openai.com/oauth/authorize?state=fixture\n",
        "https://auth.openai.com/oauth/authorize?state=fixture\r",
    ],
)
def test_login_url_contract_rejects_unapproved_destinations(
    contracts: ContractSet, url: str
) -> None:
    payload = deepcopy(contracts["get_provider_auth_status"].output_examples[0])
    payload["operation"].update(state="pending", authorization_url=url)
    with pytest.raises(ContractMismatchError):
        contracts.validate_output("get_provider_auth_status", payload)


@pytest.mark.parametrize("secret_field", ["api_key", "access_token", "credential_ref", "key_hash"])
def test_public_connection_cannot_include_secret_material(
    contracts: ContractSet, secret_field: str
) -> None:
    payload = deepcopy(contracts["set_provider_connection"].output_examples[0])
    payload["connection"][secret_field] = "fake-secret-for-contract"
    with pytest.raises(ContractMismatchError):
        contracts.validate_output("set_provider_connection", payload)


def test_codex_public_metadata_declares_only_official_chatgpt_auth(
    contracts: ContractSet,
) -> None:
    payload = deepcopy(contracts["set_provider_connection"].output_examples[0])
    payload["connection"].update(
        provider_id="codex-app-server", auth_method="chatgpt", endpoint="https://chatgpt.com"
    )
    contracts.validate_output("set_provider_connection", payload)
    payload["connection"]["auth_method"] = "api_key"
    with pytest.raises(ContractMismatchError):
        contracts.validate_output("set_provider_connection", payload)
    payload["connection"].update(auth_method="chatgpt", endpoint="https://api.openai.com")
    with pytest.raises(ContractMismatchError):
        contracts.validate_output("set_provider_connection", payload)

    descriptor = deepcopy(contracts["list_providers"].output_examples[0]["providers"][0])
    descriptor.update(provider_id="codex-app-server", auth_methods=["chatgpt"])
    contracts.validate_output("list_providers", {"providers": [descriptor]})
    descriptor["auth_methods"] = ["api_key", "chatgpt"]
    with pytest.raises(ContractMismatchError):
        contracts.validate_output("list_providers", {"providers": [descriptor]})


def test_codex_model_catalog_accepts_official_reasoning_values(contracts: ContractSet) -> None:
    payload = deepcopy(contracts["list_provider_models"].output_examples[0])
    model = payload["models"][0]
    model.update(source="runtime", availability="available", reason=None)
    model["billing"]["kind"] = "subscription"
    model["parameter_schema"] = {
        "type": "object",
        "properties": {"reasoning_effort": {"type": "string", "enum": ["low", "medium", "high"]}},
        "required": [],
        "additionalProperties": False,
    }
    contracts.validate_output("list_provider_models", payload)


@pytest.mark.parametrize(
    "changes",
    [
        {"input_modalities": ["unknown"]},
        {"timestamp_support": "invented"},
        {"context_window": 0},
        {"availability": "probably_available"},
        {"source": "ambient_environment"},
        {"billing": {"kind": "free"}},
    ],
)
def test_model_metadata_rejects_unknown_or_invalid_capabilities(
    contracts: ContractSet, changes: dict[str, Any]
) -> None:
    payload = deepcopy(contracts["list_provider_models"].output_examples[0])
    payload["models"][0].update(changes)
    with pytest.raises(ContractMismatchError):
        contracts.validate_output("list_provider_models", payload)


@pytest.mark.parametrize(
    "parameter", ["api_key", "token", "url", "command", "env", "path", "headers"]
)
def test_model_parameters_cannot_advertise_secret_or_runtime_injection(
    contracts: ContractSet, parameter: str
) -> None:
    payload = deepcopy(contracts["list_provider_models"].output_examples[0])
    payload["models"][0]["parameter_schema"]["properties"][parameter] = {"type": "string"}
    with pytest.raises(ContractMismatchError):
        contracts.validate_output("list_provider_models", payload)


@pytest.mark.parametrize("price", [-1, 0.5, "-1", "NaN", "unknown"])
def test_unknown_prices_are_null_not_guessed_values(contracts: ContractSet, price: Any) -> None:
    payload = deepcopy(contracts["list_provider_models"].output_examples[0])
    payload["models"][0]["billing"]["input_usd_per_million_tokens"] = price
    with pytest.raises(ContractMismatchError):
        contracts.validate_output("list_provider_models", payload)


def test_download_catalog_requires_verified_digest_and_destination(contracts: ContractSet) -> None:
    resource = deepcopy(
        contracts["list_providers"].output_examples[0]["providers"][1]["runtime"]["resources"][0]
    )
    validator = Draft202012Validator(contracts.schema_for_def("provider_runtime_resource"))
    validator.validate(resource)
    resource["source"] = "approved_download"
    with pytest.raises(ValidationError):
        validator.validate(resource)
    resource.update(version="1.0.0", sha256="0" * 64, download_host="runtime.example")
    validator.validate(resource)


def test_v2_server_metadata_requires_workflow_and_transport_disclosure(
    contracts: ContractSet,
) -> None:
    payload = deepcopy(contracts["get_server_info"].output_examples[0])
    del payload["capabilities"]["workflow"]
    with pytest.raises(ContractMismatchError):
        contracts.validate_output("get_server_info", payload)
    payload = deepcopy(contracts["get_server_info"].output_examples[0])
    del payload["secure_transport"]
    with pytest.raises(ContractMismatchError):
        contracts.validate_output("get_server_info", payload)


@pytest.mark.parametrize("selection", [None, MINUTES_MODEL])
def test_minutes_model_is_shared_across_meeting_and_profile_tools(
    contracts: ContractSet, selection: dict[str, Any] | None
) -> None:
    for tool in ("set_meeting_config", "set_profile", "start_recording", "import_recording"):
        args = dict(contracts[tool].input_examples[0])
        if tool == "set_meeting_config":
            args.update(minutes_model=selection, external_send_policy="subscription_ok")
        else:
            args["config"] = {"minutes_model": selection, "external_send_policy": "subscription_ok"}
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
        config.update(minutes_model=selection, external_send_policy="subscription_ok")
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
        {"provider": "openai-api"},
        {"provider": "ollama"},
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
