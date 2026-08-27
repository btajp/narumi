"""Write-only input validation never interpolates raw values, even for malformed shapes."""

from __future__ import annotations

import json
from typing import Any

import pytest
from narumi.contracts import load_contracts
from narumi.errors import ContractMismatchError, InvalidArgumentError

SECRET = "fake-contract-secret-35794"
REQUEST_ID = "fake-request-id-12345"


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
def test_secret_tool_validation_uses_only_trusted_paths_and_validator_names(arguments: Any):
    contracts = load_contracts()
    with pytest.raises(InvalidArgumentError) as error:
        contracts.validate_input("set_gaia_connection", arguments)
    assert SECRET not in str(error.value)
    assert SECRET not in json.dumps(error.value.to_payload())
    for item in error.value.details["errors"]:
        assert item["path"] in {"$", "$.api_key", "$.request_id", "$.url"}
        assert item["message"] == f"validation failed: {item['validator']}"


def test_secret_output_validation_cannot_echo_a_malformed_handler_result():
    contracts = load_contracts()
    with pytest.raises(ContractMismatchError) as error:
        contracts.validate_output("set_gaia_connection", {"connection": SECRET, SECRET: SECRET})
    assert SECRET not in str(error.value)
    assert SECRET not in json.dumps(error.value.to_payload())


def test_write_only_detection_does_not_change_other_tool_diagnostics():
    contracts = load_contracts()
    assert contracts["set_gaia_connection"].has_write_only_input
    assert not contracts["get_meeting"].has_write_only_input
    with pytest.raises(InvalidArgumentError) as error:
        contracts.validate_input("get_meeting", {"meeting_id": "invalid"})
    assert error.value.details["errors"][0]["path"] == "$.meeting_id"
