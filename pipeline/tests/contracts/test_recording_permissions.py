"""Permission setup contract guards, without OS calls or a running recorder."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest
from narumi.contracts import ContractSet, load_contracts
from narumi.errors import ContractMismatchError, InvalidArgumentError

TOOL = "configure_recording_permission"
BASE = {"permission": "microphone", "action": "request", "request_id": "permission-test-001"}


@pytest.fixture(scope="module")
def contracts() -> ContractSet:
    return load_contracts()


@pytest.mark.parametrize("permission", ["microphone", "screen_recording"])
@pytest.mark.parametrize("action", ["request", "open_settings"])
def test_permission_actions_are_closed_enums(
    contracts: ContractSet, permission: str, action: str
) -> None:
    contracts.validate_input(TOOL, {**BASE, "permission": permission, "action": action})


@pytest.mark.parametrize(
    "change",
    [
        {"permission": "camera"},
        {"permission": "../../microphone"},
        {"action": "grant"},
        {"action": "reset"},
        {"request_id": "short"},
        {"url": "x-apple.systempreferences:arbitrary"},
        {"command": "record"},
        {"output": "/tmp/unrequested-recording"},
    ],
)
def test_permission_rejects_unsafe_or_unknown_arguments(
    contracts: ContractSet, change: dict[str, Any]
) -> None:
    with pytest.raises(InvalidArgumentError):
        contracts.validate_input(TOOL, {**BASE, **change})


@pytest.mark.parametrize("missing", ["permission", "action", "request_id"])
def test_permission_requires_all_arguments(contracts: ContractSet, missing: str) -> None:
    args = {key: value for key, value in BASE.items() if key != missing}
    with pytest.raises(InvalidArgumentError):
        contracts.validate_input(TOOL, args)


def test_permission_is_non_destructive_idempotent_write(contracts: ContractSet) -> None:
    assert contracts[TOOL].annotations == {
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }


@pytest.mark.parametrize("status", ["granted", "denied", "unknown"])
def test_denial_is_a_normal_result(contracts: ContractSet, status: str) -> None:
    contracts.validate_output(
        TOOL,
        {
            "permission": "microphone",
            "action": "request",
            "permissions": {"screen_recording": "denied", "microphone": status},
            "settings_opened": False,
        },
    )


@pytest.mark.parametrize("value", ["allowed", None, True, 1])
def test_permission_report_rejects_unknown_status(contracts: ContractSet, value: Any) -> None:
    result = deepcopy(contracts[TOOL].examples["output"][0])
    result["permissions"]["microphone"] = value
    with pytest.raises(ContractMismatchError):
        contracts.validate_output(TOOL, result)


@pytest.mark.parametrize(
    "args", [None, {}, {"refresh_permissions": False}, {"refresh_permissions": True}]
)
def test_server_info_refresh_is_optional_and_read_only(
    contracts: ContractSet, args: dict[str, Any] | None
) -> None:
    contracts.validate_input("get_server_info", args)
    assert contracts["get_server_info"].read_only


@pytest.mark.parametrize("value", ["true", None, 1, "request"])
def test_server_info_refresh_rejects_non_boolean(contracts: ContractSet, value: Any) -> None:
    with pytest.raises(InvalidArgumentError):
        contracts.validate_input("get_server_info", {"refresh_permissions": value})


def test_permission_busy_flag_is_optional_for_old_server(contracts: ContractSet) -> None:
    result = deepcopy(contracts["get_server_info"].examples["output"][0])
    result["capabilities"].pop("permission_setup_in_progress")
    contracts.validate_output("get_server_info", result)
    result["capabilities"]["permission_setup_in_progress"] = True
    contracts.validate_output("get_server_info", result)
    result["capabilities"]["permission_setup_in_progress"] = "false"
    with pytest.raises(ContractMismatchError):
        contracts.validate_output("get_server_info", result)


def test_server_instance_id_is_optional_for_old_server(contracts: ContractSet) -> None:
    result = deepcopy(contracts["get_server_info"].examples["output"][0])
    contracts.validate_output("get_server_info", result)
    result.pop("server_instance_id")
    contracts.validate_output("get_server_info", result)


@pytest.mark.parametrize(
    "value",
    [None, "", 1, True, "same-server", "00000000-0000-0000-0000-000000000001"],
)
def test_server_instance_id_rejects_invalid_identity(contracts: ContractSet, value: Any) -> None:
    result = deepcopy(contracts["get_server_info"].examples["output"][0])
    result["server_instance_id"] = value
    with pytest.raises(ContractMismatchError):
        contracts.validate_output("get_server_info", result)
