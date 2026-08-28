"""Secret and transient device-code validation never exposes raw malformed values."""

from __future__ import annotations

import json
import traceback
from copy import deepcopy
from typing import Any

import pytest
from narumi.contracts import ContractSet, load_contracts
from narumi.errors import ContractMismatchError, InvalidArgumentError, NarumiError

SECRET = "fake-contract-secret-35794"
REQUEST_ID = "fake-request-id-12345"
DEVICE_AUTH_TOOLS = ("authenticate_provider_connection", "get_provider_auth_status")
DEVICE_URL = "https://auth.openai.com/codex/device"


@pytest.fixture(scope="module")
def contracts() -> ContractSet:
    return load_contracts()


def _assert_safe_validation_error(error: NarumiError, schema: dict[str, Any]) -> None:
    renderings = (
        error.message,
        str(error),
        repr(error),
        json.dumps(error.to_payload()),
        "".join(traceback.format_exception(error)),
    )
    for rendering in renderings:
        assert SECRET not in rendering
    trusted_paths = {"$"} | {f"$.{name}" for name in schema["properties"]}
    for item in error.details["errors"]:
        assert item["path"] in trusted_paths
        assert item["message"] == f"validation failed: {item['validator']}"


@pytest.mark.parametrize("tool", ["set_gaia_connection", "set_provider_connection"])
@pytest.mark.parametrize(
    "arguments",
    [
        {"api_key": {SECRET: SECRET}, "request_id": REQUEST_ID},
        {"api_key": [SECRET], "request_id": REQUEST_ID},
        {"api_key": SECRET * 500, "request_id": REQUEST_ID},
        {"api_key": SECRET, "request_id": {SECRET: SECRET}},
        {"api_key": SECRET, "request_id": REQUEST_ID, SECRET: SECRET},
        {"url": {SECRET: SECRET}, "request_id": REQUEST_ID},
        [SECRET],
        SECRET,
    ],
)
def test_secret_tool_validation_uses_only_trusted_paths_and_validator_names(
    tool: str, arguments: Any
):
    contracts = load_contracts()
    with pytest.raises(InvalidArgumentError) as error:
        contracts.validate_input(tool, arguments)
    assert SECRET not in str(error.value)
    assert SECRET not in json.dumps(error.value.to_payload())
    trusted_paths = {"$"} | {f"$.{name}" for name in contracts[tool].input_schema["properties"]}
    for item in error.value.details["errors"]:
        assert item["path"] in trusted_paths
        assert item["message"] == f"validation failed: {item['validator']}"


@pytest.mark.parametrize("tool", ["set_gaia_connection", "set_provider_connection"])
def test_secret_output_validation_cannot_echo_a_malformed_handler_result(tool: str):
    contracts = load_contracts()
    with pytest.raises(ContractMismatchError) as error:
        contracts.validate_output(tool, {"connection": SECRET, SECRET: SECRET})
    assert SECRET not in str(error.value)
    assert SECRET not in json.dumps(error.value.to_payload())


def test_write_only_detection_does_not_change_other_tool_diagnostics():
    contracts = load_contracts()
    assert contracts["set_gaia_connection"].has_write_only_input
    assert contracts["set_provider_connection"].has_write_only_input
    assert not contracts["get_meeting"].has_write_only_input
    with pytest.raises(InvalidArgumentError) as error:
        contracts.validate_input("get_meeting", {"meeting_id": "invalid"})
    assert error.value.details["errors"][0]["path"] == "$.meeting_id"


@pytest.mark.parametrize("tool", DEVICE_AUTH_TOOLS)
@pytest.mark.parametrize(
    "arguments",
    [
        {"user_code": SECRET},
        {"authorization_url": f"{DEVICE_URL}?code={SECRET}"},
        {"connection_id": {SECRET: SECRET}},
        {SECRET: SECRET},
        [SECRET],
        SECRET,
    ],
)
def test_device_auth_input_validation_redacts_unknown_keys_and_values(
    contracts: ContractSet, tool: str, arguments: Any
) -> None:
    with pytest.raises(InvalidArgumentError) as error:
        contracts.validate_input(tool, arguments)
    _assert_safe_validation_error(error.value, contracts[tool].input_schema)


@pytest.mark.parametrize("tool", DEVICE_AUTH_TOOLS)
@pytest.mark.parametrize(
    "changes",
    [
        {"user_code": SECRET * 2},
        {"user_code": {SECRET: SECRET}},
        {"user_code": [SECRET]},
        {"user_code": f"{SECRET}\n"},
        {"authorization_url": f"{DEVICE_URL}?code={SECRET}"},
        {"authorization_url": None},
        {"state": "succeeded"},
        {SECRET: SECRET},
    ],
)
def test_device_auth_output_validation_redacts_codes_urls_and_untrusted_paths(
    contracts: ContractSet, tool: str, changes: dict[str, Any]
) -> None:
    payload = deepcopy(contracts[tool].output_examples[0])
    payload["operation"].update(state="pending", authorization_url=DEVICE_URL, user_code=SECRET)
    payload["operation"].update(changes)
    with pytest.raises(ContractMismatchError) as error:
        contracts.validate_output(tool, payload)
    _assert_safe_validation_error(error.value, contracts[tool].output_schema)


@pytest.mark.parametrize("tool", DEVICE_AUTH_TOOLS)
@pytest.mark.parametrize("result", [{"operation": SECRET}, {SECRET: SECRET}, [SECRET], SECRET])
def test_device_auth_output_validation_redacts_malformed_response_shapes(
    contracts: ContractSet, tool: str, result: Any
) -> None:
    with pytest.raises(ContractMismatchError) as error:
        contracts.validate_output(tool, result)
    _assert_safe_validation_error(error.value, contracts[tool].output_schema)


@pytest.mark.parametrize("tool", DEVICE_AUTH_TOOLS)
@pytest.mark.parametrize("output", [False, True])
def test_device_auth_validation_suppresses_an_existing_secret_exception_chain(
    contracts: ContractSet, tool: str, output: bool
) -> None:
    error_type = ContractMismatchError if output else InvalidArgumentError
    validate = contracts.validate_output if output else contracts.validate_input
    with pytest.raises(error_type) as error:
        try:
            raise ValueError(SECRET)
        except ValueError:
            validate(tool, {"user_code": SECRET})
    schema = contracts[tool].output_schema if output else contracts[tool].input_schema
    _assert_safe_validation_error(error.value, schema)
    assert error.value.__suppress_context__


@pytest.mark.parametrize("tool", DEVICE_AUTH_TOOLS)
def test_device_code_output_does_not_change_write_only_input_handling(
    contracts: ContractSet, tool: str
) -> None:
    assert not contracts[tool].has_write_only_input
    contracts.validate_input(tool, contracts[tool].input_examples[0])
    payload = deepcopy(contracts[tool].output_examples[0])
    payload["operation"].update(state="pending", authorization_url=DEVICE_URL, user_code=SECRET)
    contracts.validate_output(tool, payload)
