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


@pytest.mark.parametrize("provider", ["anthropic-api", "claude-agent-sdk", "ollama"])
def test_new_connection_can_be_saved_before_authentication(
    contracts: ContractSet, provider: str
) -> None:
    args = {**CREATE, "provider_id": provider}
    if provider == "ollama":
        args.update(auth_method="none", endpoint="http://127.0.0.1:11434")
    contracts.validate_input("set_provider_connection", args)


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


@pytest.mark.parametrize("secret_field", ["api_key", "access_token", "credential_ref", "key_hash"])
def test_public_connection_cannot_include_secret_material(
    contracts: ContractSet, secret_field: str
) -> None:
    payload = deepcopy(contracts["set_provider_connection"].output_examples[0])
    payload["connection"][secret_field] = "fake-secret-for-contract"
    with pytest.raises(ContractMismatchError):
        contracts.validate_output("set_provider_connection", payload)


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
