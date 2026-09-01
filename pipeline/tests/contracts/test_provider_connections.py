"""Provider setup contracts: closed inputs, versioned mutations and safe public metadata."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest
from jsonschema import Draft202012Validator, ValidationError
from narumi.contracts import ContractSet, load_contracts
from narumi.errors import ContractMismatchError, InvalidArgumentError

CONNECTION_ID = "conn-0123456789ab"
OPERATION_ID = "auth-0123456789ab"
REQUEST_ID = "provider-contract-request-001"
AUTHORIZATION_URL = "https://auth.openai.com/codex/device"
USER_CODE = "ABCD-EFGH"
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
    "provider",
    [
        "codex-app-server",
        "claude-agent-sdk",
        "openai-api",
        "openai-compatible-api",
        "anthropic-api",
        "ollama",
    ],
)
def test_new_connection_can_be_saved_before_authentication(
    contracts: ContractSet, provider: str
) -> None:
    args = {**CREATE, "provider_id": provider}
    if provider == "ollama":
        args.update(auth_method="none", endpoint="http://127.0.0.1:11434")
    elif provider == "codex-app-server":
        args.update(auth_method="chatgpt", endpoint="https://chatgpt.com")
    elif provider == "openai-compatible-api":
        args.update(endpoint="https://llm.example.com/v1", api_surface="responses")
    contracts.validate_input("set_provider_connection", args)


def test_provider_catalog_uses_the_canonical_six_provider_order(contracts: ContractSet) -> None:
    payload = deepcopy(contracts["list_providers"].output_examples[0])
    assert [provider["provider_id"] for provider in payload["providers"]] == [
        "codex-app-server",
        "claude-agent-sdk",
        "openai-api",
        "openai-compatible-api",
        "anthropic-api",
        "ollama",
    ]
    contracts.validate_output("list_providers", payload)
    compatible = payload["providers"][3]
    assert compatible["display_name"] == "OpenAI互換API"
    assert compatible["auth_methods"] == ["api_key", "none"]
    assert compatible["roles"] == ["llm"]
    missing = deepcopy(payload)
    missing["providers"].pop(3)
    with pytest.raises(ContractMismatchError):
        contracts.validate_output("list_providers", missing)
    reordered = deepcopy(payload)
    reordered["providers"][0], reordered["providers"][1] = (
        reordered["providers"][1],
        reordered["providers"][0],
    )
    with pytest.raises(ContractMismatchError):
        contracts.validate_output("list_providers", reordered)


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
    "changes",
    [
        {"auth_method": "chatgpt"},
        {"auth_method": "none"},
        {"endpoint": "https://api.openai.com/v1"},
        {"endpoint": "https://api.openai.com/"},
        {"endpoint": "http://api.openai.com"},
        {"endpoint": "https://api.openai.com.evil.example"},
        {"endpoint": "https://api.openai.com?api_key=example"},
        {"endpoint": "https://example@api.openai.com"},
        {"organization": "unapproved-override"},
        {"project": "unapproved-override"},
    ],
)
def test_openai_connections_require_api_key_auth_and_the_fixed_endpoint(
    contracts: ContractSet, changes: dict[str, Any]
) -> None:
    with pytest.raises(InvalidArgumentError):
        contracts.validate_input(
            "set_provider_connection", {**CREATE, "provider_id": "openai-api", **changes}
        )


@pytest.mark.parametrize("key", [None, "example-not-a-real-key"])
def test_openai_connection_can_save_without_authenticating(
    contracts: ContractSet, key: str | None
) -> None:
    contracts.validate_input(
        "set_provider_connection",
        {
            **CREATE,
            "provider_id": "openai-api",
            "endpoint": "https://api.openai.com",
            "api_key": key,
        },
    )


def test_openai_public_connection_and_runtime_metadata(contracts: ContractSet) -> None:
    connection = deepcopy(contracts["set_provider_connection"].output_examples[0]["connection"])
    connection.update(
        provider_id="openai-api",
        endpoint="https://api.openai.com",
        auth_method="api_key",
        api_surface="responses",
    )
    contracts.validate_output("set_provider_connection", {"connection": connection})
    connection["endpoint"] = "https://api.anthropic.com"
    with pytest.raises(ContractMismatchError):
        contracts.validate_output("set_provider_connection", {"connection": connection})
    connection.update(endpoint="https://api.openai.com", api_surface="chat_completions")
    with pytest.raises(ContractMismatchError):
        contracts.validate_output("set_provider_connection", {"connection": connection})

    descriptor = deepcopy(contracts["list_providers"].output_examples[0]["providers"][0])
    descriptor.update(
        provider_id="openai-api", auth_methods=["api_key"], roles=["llm", "transcription"]
    )
    descriptor_validator = Draft202012Validator(contracts.schema_for_def("provider_descriptor"))
    descriptor_validator.validate(descriptor)
    descriptor["auth_methods"] = ["chatgpt"]
    with pytest.raises(ValidationError):
        descriptor_validator.validate(descriptor)
    descriptor.update(auth_methods=["api_key"], roles=["transcription"])
    with pytest.raises(ValidationError):
        descriptor_validator.validate(descriptor)

    preparation = deepcopy(contracts["prepare_provider_runtime"].input_examples[0])
    preparation.update(provider_id="openai-api", resource_id="openai-client")
    contracts.validate_input("prepare_provider_runtime", preparation)


@pytest.mark.parametrize(
    ("endpoint", "auth_method"),
    [
        ("https://llm.example.com", "api_key"),
        ("https://llm.example.com/v1", "api_key"),
        ("https://gateway.example.com/openai/v1", "api_key"),
        ("http://127.0.0.1:8080/v1", "none"),
        ("https://127.22.1.255:443/v1", "none"),
        ("http://[::1]:11434/v1", "none"),
    ],
)
@pytest.mark.parametrize("api_surface", ["responses", "chat_completions"])
def test_openai_compatible_connection_accepts_explicit_safe_configuration(
    contracts: ContractSet, endpoint: str, auth_method: str, api_surface: str
) -> None:
    args: dict[str, Any] = {
        **CREATE,
        "provider_id": "openai-compatible-api",
        "endpoint": endpoint,
        "auth_method": auth_method,
        "api_surface": api_surface,
    }
    if api_surface == "chat_completions":
        args["chat_max_tokens_field"] = "max_completion_tokens"
    contracts.validate_input("set_provider_connection", args)


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://llm.example.com/v1",
        "https://localhost/v1",
        "https://192.168.1.10/v1",
        "http://192.168.1.10/v1",
        "https://user:secret@llm.example.com/v1",
        "https://llm.example.com/v1/",
        "https://llm.example.com//v1",
        "https://llm.example.com/./v1",
        "https://llm.example.com/../v1",
        "https://llm.example.com/%2e%2e/v1",
        "https://llm.example.com/v1?key=secret",
        "https://llm.example.com/v1#fragment",
        "https://llm.example.com:0/v1",
        "https://llm.example.com:65536/v1",
        "https://llm.example.com\\v1",
    ],
)
def test_openai_compatible_connection_rejects_unsafe_base_endpoints(
    contracts: ContractSet, endpoint: str
) -> None:
    with pytest.raises(InvalidArgumentError):
        contracts.validate_input(
            "set_provider_connection",
            {
                **CREATE,
                "provider_id": "openai-compatible-api",
                "endpoint": endpoint,
                "api_surface": "responses",
            },
        )


def test_openai_compatible_none_auth_is_numeric_loopback_only(contracts: ContractSet) -> None:
    with pytest.raises(InvalidArgumentError):
        contracts.validate_input(
            "set_provider_connection",
            {
                **CREATE,
                "provider_id": "openai-compatible-api",
                "endpoint": "https://llm.example.com/v1",
                "auth_method": "none",
                "api_surface": "responses",
            },
        )


@pytest.mark.parametrize("field", ["max_tokens", "max_completion_tokens"])
def test_openai_compatible_chat_requires_an_exact_token_field(
    contracts: ContractSet, field: str
) -> None:
    args = {
        **CREATE,
        "provider_id": "openai-compatible-api",
        "endpoint": "https://llm.example.com/v1",
        "api_surface": "chat_completions",
        "chat_max_tokens_field": field,
    }
    contracts.validate_input("set_provider_connection", args)
    with pytest.raises(InvalidArgumentError):
        contracts.validate_input(
            "set_provider_connection",
            {key: value for key, value in args.items() if key != "chat_max_tokens_field"},
        )


def test_openai_compatible_surface_update_can_clear_chat_token_field(
    contracts: ContractSet,
) -> None:
    contracts.validate_input(
        "set_provider_connection",
        {
            **UPDATE,
            "api_surface": "responses",
            "chat_max_tokens_field": None,
        },
    )
    with pytest.raises(InvalidArgumentError):
        contracts.validate_input(
            "set_provider_connection",
            {
                **UPDATE,
                "api_surface": "responses",
                "chat_max_tokens_field": "max_tokens",
            },
        )
    with pytest.raises(InvalidArgumentError):
        contracts.validate_input(
            "set_provider_connection",
            {**UPDATE, "api_surface": "chat_completions"},
        )
    with pytest.raises(InvalidArgumentError):
        contracts.validate_input(
            "set_provider_connection",
            {**UPDATE, "endpoint": "https://user:secret@llm.example.com/v1"},
        )


def test_openai_compatible_public_connection_exposes_only_non_secret_protocol_config(
    contracts: ContractSet,
) -> None:
    connection = deepcopy(contracts["set_provider_connection"].output_examples[-1]["connection"])
    contracts.validate_output("set_provider_connection", {"connection": connection})
    connection.update(api_surface="chat_completions", chat_max_tokens_field="max_completion_tokens")
    contracts.validate_output("set_provider_connection", {"connection": connection})
    connection["api_surface"] = "responses"
    with pytest.raises(ContractMismatchError):
        contracts.validate_output("set_provider_connection", {"connection": connection})


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
        {"user_code": USER_CODE},
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
def test_only_pending_authentication_start_can_publish_a_device_login_pair(
    contracts: ContractSet, tool: str
) -> None:
    payload = deepcopy(contracts[tool].output_examples[0])
    payload["operation"].update(state="pending", authorization_url=None, user_code=None)
    contracts.validate_output(tool, payload)
    payload["operation"].update(authorization_url=AUTHORIZATION_URL, user_code=USER_CODE)
    contracts.validate_output(tool, payload)
    for state in ("succeeded", "failed", "cancelled", "unknown"):
        payload["operation"]["state"] = state
        with pytest.raises(ContractMismatchError):
            contracts.validate_output(tool, payload)
        payload["operation"].update(authorization_url=None, user_code=None)
        contracts.validate_output(tool, payload)
        payload["operation"].update(authorization_url=AUTHORIZATION_URL, user_code=USER_CODE)
    payload["operation"].update(state="pending", action="logout")
    with pytest.raises(ContractMismatchError):
        contracts.validate_output(tool, payload)
    payload["operation"]["action"] = "cancel"
    with pytest.raises(ContractMismatchError):
        contracts.validate_output(tool, payload)


@pytest.mark.parametrize(
    "url",
    [
        "http://auth.openai.com/codex/device",
        "https://chatgpt.com/codex/device",
        "https://auth.openai.com.evil.example/codex/device",
        "https://evil.example@auth.openai.com/codex/device",
        "https://auth.openai.com:443/codex/device",
        "https://auth.openai.com/codex/device/",
        "https://auth.openai.com/codex/device?user_code=ABCD-EFGH",
        "https://auth.openai.com/codex/device#fragment",
        "https://auth.openai.com/codex/device\n",
        "https://auth.openai.com/codex/device\r",
        "https://auth.openai.com/oauth/authorize?state=fixture",
        "http://localhost:1455/auth/callback",
    ],
)
def test_login_url_contract_rejects_unapproved_destinations(
    contracts: ContractSet, url: str
) -> None:
    payload = deepcopy(contracts["get_provider_auth_status"].output_examples[0])
    payload["operation"].update(state="pending", authorization_url=url, user_code=USER_CODE)
    with pytest.raises(ContractMismatchError):
        contracts.validate_output("get_provider_auth_status", payload)


@pytest.mark.parametrize("user_code", ["A", "abcd-1234", "A1-" * 10 + "AB"])
def test_device_code_allows_only_bounded_ascii_display_codes(
    contracts: ContractSet, user_code: str
) -> None:
    payload = deepcopy(contracts["get_provider_auth_status"].output_examples[0])
    payload["operation"].update(
        state="pending", authorization_url=AUTHORIZATION_URL, user_code=user_code
    )
    contracts.validate_output("get_provider_auth_status", payload)


@pytest.mark.parametrize(
    "user_code", [None, "", "A" * 33, "確認コード", "A_B", "AB CD", "ABC\n", "ABC\r", 1234, []]
)
def test_device_code_rejects_invalid_or_unpaired_codes(
    contracts: ContractSet, user_code: Any
) -> None:
    payload = deepcopy(contracts["get_provider_auth_status"].output_examples[0])
    payload["operation"].update(
        state="pending", authorization_url=AUTHORIZATION_URL, user_code=user_code
    )
    with pytest.raises(ContractMismatchError):
        contracts.validate_output("get_provider_auth_status", payload)


@pytest.mark.parametrize("tool", ["authenticate_provider_connection", "get_provider_auth_status"])
def test_auth_operation_requires_an_explicit_nullable_device_code(
    contracts: ContractSet, tool: str
) -> None:
    payload = deepcopy(contracts[tool].output_examples[0])
    payload["operation"].update(state="pending", authorization_url=None, user_code=USER_CODE)
    with pytest.raises(ContractMismatchError):
        contracts.validate_output(tool, payload)
    del payload["operation"]["user_code"]
    with pytest.raises(ContractMismatchError):
        contracts.validate_output(tool, payload)


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
    descriptor_validator = Draft202012Validator(contracts.schema_for_def("provider_descriptor"))
    descriptor_validator.validate(descriptor)
    descriptor["auth_methods"] = ["api_key", "chatgpt"]
    with pytest.raises(ValidationError):
        descriptor_validator.validate(descriptor)


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
        {"availability_expires_on": 0},
        {"availability_expires_on": False},
        {"availability_expires_on": 1.5},
        {"availability_expires_on": ""},
        {"availability_expires_on": "later"},
        {"availability_expires_on": "2026-9-30"},
        {"availability_expires_on": "20260930"},
        {"availability_expires_on": "2026-09-30\n"},
        {"availability_expires_on": "2026-09-30T00:00:00Z"},
        {"availability_expires_on": "2026-09-30T00:00:00"},
        {"availability_expires_on": "2026-09-31"},
        {"availability_expires_on": "2026-02-29"},
        {"availability_expires_on": "2026-13-01"},
        {"availability_expires_on": "0000-01-01"},
        {"availability_expires_on": []},
        {"availability_expires_on": {}},
        {"availability_expires_at": "2026-09-30T00:00:00Z"},
    ],
)
def test_model_metadata_rejects_unknown_or_invalid_capabilities(
    contracts: ContractSet, changes: dict[str, Any]
) -> None:
    payload = deepcopy(contracts["list_provider_models"].output_examples[0])
    payload["models"][0].update(changes)
    with pytest.raises(ContractMismatchError):
        contracts.validate_output("list_provider_models", payload)


@pytest.mark.parametrize("end_date", [None, "2026-08-01", "2026-09-30", "2028-02-29"])
def test_model_availability_end_date_is_optional_and_not_compared_by_schema(
    contracts: ContractSet, end_date: str | None
) -> None:
    payload = deepcopy(contracts["list_provider_models"].output_examples[0])
    payload["models"][0]["availability_expires_on"] = end_date
    contracts.validate_output("list_provider_models", payload)
    del payload["models"][0]["availability_expires_on"]
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


def test_server_metadata_requires_workflow_and_transport_disclosure(
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


def test_server_metadata_requires_exact_resident_minutes_providers(contracts: ContractSet) -> None:
    payload = deepcopy(contracts["get_server_info"].output_examples[0])
    contracts.validate_output("get_server_info", payload)
    payload["capabilities"]["minutes_model_providers"] = []
    with pytest.raises(ContractMismatchError):
        contracts.validate_output("get_server_info", payload)
    payload = deepcopy(contracts["get_server_info"].output_examples[0])
    del payload["capabilities"]["minutes_model_providers"]
    with pytest.raises(ContractMismatchError):
        contracts.validate_output("get_server_info", payload)


@pytest.mark.parametrize(
    "providers",
    [
        ["codex-app-server", "codex-app-server"],
        ["claude-agent-sdk"],
        ["unknown-provider"],
        ["fake"],
        "openai-api",
        None,
    ],
)
def test_minutes_provider_capabilities_are_a_closed_unique_list(
    contracts: ContractSet, providers: Any
) -> None:
    payload = deepcopy(contracts["get_server_info"].output_examples[0])
    payload["capabilities"]["minutes_model_providers"] = providers
    with pytest.raises(ContractMismatchError):
        contracts.validate_output("get_server_info", payload)


def test_model_verification_requires_literal_charge_confirmation(
    contracts: ContractSet,
) -> None:
    example = contracts["verify_provider_model"].input_examples[0]
    contracts.validate_input("verify_provider_model", example)
    for confirmation in (None, "yes", "send_test_prompt", True):
        with pytest.raises(InvalidArgumentError):
            contracts.validate_input(
                "verify_provider_model", {**example, "confirmation": confirmation}
            )


def test_compatible_and_claude_metadata_candidates_require_explicit_verification(
    contracts: ContractSet,
) -> None:
    examples = contracts["list_provider_models"].output_examples
    for connection_id in ("conn-222233334444", "conn-333344445555"):
        payload = next(item for item in examples if item["connection_id"] == connection_id)
        contracts.validate_output("list_provider_models", payload)
        assert payload["models"]
        assert all(model["availability"] == "unverified" for model in payload["models"])
        assert all(
            model["reason"] == "explicit_model_verification_required" for model in payload["models"]
        )


@pytest.mark.parametrize(
    "extra",
    [
        {"prompt": "caller controlled"},
        {"parameters": {"max_tokens": 10}},
        {"provider_id": "openai-compatible-api"},
        {"retry": True},
        {"fallback_model": "another-model"},
        {"api_key": "example-not-a-real-key"},
    ],
)
def test_model_verification_accepts_no_prompt_secret_retry_or_fallback(
    contracts: ContractSet, extra: dict[str, Any]
) -> None:
    with pytest.raises(InvalidArgumentError):
        contracts.validate_input(
            "verify_provider_model",
            {**contracts["verify_provider_model"].input_examples[0], **extra},
        )


def test_successful_model_verification_returns_an_available_exact_descriptor(
    contracts: ContractSet,
) -> None:
    payload = deepcopy(contracts["verify_provider_model"].output_examples[0])
    contracts.validate_output("verify_provider_model", payload)
    payload["model"]["availability"] = "unverified"
    payload["model"]["reason"] = "adapter_capability_verification_required"
    with pytest.raises(ContractMismatchError):
        contracts.validate_output("verify_provider_model", payload)


def test_model_verification_is_write_idempotent_and_open_world(contracts: ContractSet) -> None:
    tool = contracts["verify_provider_model"]
    assert tool.annotations == {
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
    assert "request_id" in tool.input_schema["required"]
