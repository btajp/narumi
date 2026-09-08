"""Public recording selection, playback, and interruption contracts."""

from copy import deepcopy

import pytest
from narumi.contracts import load_contracts
from narumi.errors import ContractMismatchError, InvalidArgumentError


@pytest.mark.parametrize("display_id", [1, 4294967295])
def test_explicit_display_id_is_accepted(display_id):
    load_contracts().validate_input(
        "start_recording", {"request_id": "display-contract-test", "display_id": display_id}
    )


@pytest.mark.parametrize("display_id", [0, -1, 4294967296, True, "1", None, 1.5])
def test_invalid_display_id_cannot_fall_back_to_another_screen(display_id):
    with pytest.raises(InvalidArgumentError):
        load_contracts().validate_input(
            "start_recording", {"request_id": "display-contract-test", "display_id": display_id}
        )


def test_display_list_identifies_main_display():
    contracts = load_contracts()
    result = deepcopy(contracts["list_recording_displays"].output_examples[0])
    contracts.validate_output("list_recording_displays", result)
    del result["displays"][0]["is_main"]
    with pytest.raises(ContractMismatchError):
        contracts.validate_output("list_recording_displays", result)


def test_playback_preparation_has_no_processing_or_external_send_options():
    contracts = load_contracts()
    args = contracts["prepare_recording"].input_examples[0]
    contracts.validate_input("prepare_recording", args)
    for extra in ({"auto_process": True}, {"destination": "notion"}, {"force": True}):
        with pytest.raises(InvalidArgumentError):
            contracts.validate_input("prepare_recording", {**args, **extra})


def test_stop_warning_and_playback_are_independent_of_original_tracks():
    contracts = load_contracts()
    result = deepcopy(contracts["stop_recording"].output_examples[0])
    result["recorder_error"] = {
        "code": "recorder_unavailable",
        "message": "capture ended before stop was requested",
    }
    contracts.validate_output("stop_recording", result)
    detail = deepcopy(contracts["get_meeting"].output_examples[0])
    detail["playback"] = {
        "path": "/Users/me/meetings/session/playback/recording.mp4",
        "sha256": "a" * 64,
        "bytes": 2048,
    }
    detail["recording"]["recorder_error"] = result["recorder_error"]
    contracts.validate_output("get_meeting", detail)
    detail["playback"] = None
    contracts.validate_output("get_meeting", detail)


def test_app_quit_can_defer_playback_without_processing():
    contracts = load_contracts()
    args = {"request_id": "quit-recording-test", "prepare_playback": False}
    contracts.validate_input("stop_recording", {**args, "auto_process": False})
    for extra in ({}, {"auto_process": True}):
        with pytest.raises(InvalidArgumentError):
            contracts.validate_input("stop_recording", {**args, **extra})


def test_ended_capture_keeps_explicit_finalization_path():
    contracts = load_contracts()
    contracts.validate_output(
        "get_recording_status",
        {
            "active": True,
            "recorder_alive": False,
            "meeting_id": "20260827T030500Z-a1b2c3d4",
            "tracks": {},
        },
    )
