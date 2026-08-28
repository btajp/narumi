"""Permission helpers use fake subprocesses; never invoke macOS permissions or recording."""

from __future__ import annotations

import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from conftest import FAKE_RECORDER
from narumi.bundle import Bundle
from narumi.errors import BusyError, InvalidArgumentError, RecorderUnavailableError
from narumi_server import recording
from narumi_server.permission_setup import PERMISSION_ACTION_TIMEOUT, parse_permission_result
from narumi_server.recording import RecordingController

GRANTED = {"screen_recording": "granted", "microphone": "granted"}
DENIED = {"screen_recording": "denied", "microphone": "denied"}


@pytest.fixture(autouse=True)
def isolated_environment(home: Path) -> None:
    """The shared fixture clears all fake-recorder knobs and uses an isolated data root."""


def wait_marker(marker: Path, *, timeout: float = 5) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            return int(json.loads(marker.read_text().splitlines()[0])["pid"])
        except (OSError, ValueError, IndexError):
            time.sleep(0.01)
    pytest.fail("fake permission helper did not start")


def result(*, permission: str = "microphone", action: str = "request") -> dict:
    return {
        "permission": permission,
        "action": action,
        "permissions": GRANTED,
        "settings_opened": action == "open_settings",
    }


@pytest.mark.parametrize("permission", ["microphone", "screen_recording"])
@pytest.mark.parametrize("action", ["request", "open_settings"])
def test_permission_action_roundtrip(permission: str, action: str, tmp_path: Path):
    ctl = RecordingController(FAKE_RECORDER)
    before = list(tmp_path.rglob("*"))
    assert ctl.configure_permission(permission, action) == result(
        permission=permission, action=action
    )
    assert not ctl.permission_setup_in_progress and not ctl.is_active and not ctl.process_alive
    assert list(tmp_path.rglob("*")) == before  # no bundle, output directory, tracks or logs


def test_denied_request_is_success_and_cache_is_invalidated(monkeypatch: pytest.MonkeyPatch):
    ctl = RecordingController(FAKE_RECORDER)
    assert ctl.permissions() == GRANTED
    monkeypatch.setenv("FAKE_RECORDER_CHECK", json.dumps(DENIED))
    outcome = ctl.configure_permission("microphone", "request")
    assert outcome["permissions"] == DENIED
    assert ctl.permissions() == DENIED
    assert not ctl.available()


@pytest.mark.parametrize(
    "replacement",
    [
        {"permission": "screen_recording"},
        {"action": "open_settings"},
        {"settings_opened": True},
        {"settings_opened": 0},
        {"permissions": {"screen_recording": "granted", "microphone": []}},
        {"permissions": {"screen_recording": "granted", "microphone": "maybe"}},
        {"permissions": {"microphone": "granted"}},
        {"permissions": {**GRANTED, "camera": "granted"}},
        {"unexpected": "field"},
    ],
)
def test_strict_permission_response_validation(replacement: dict):
    with pytest.raises(RecorderUnavailableError):
        parse_permission_result(
            json.dumps({**result(), **replacement}).encode(), "microphone", "request"
        )


@pytest.mark.parametrize("data", [b"", b"broken", b"null", b"[]", b"\xff", b"{}\n{}"])
def test_invalid_json_is_recorder_unavailable(data: bytes):
    with pytest.raises(RecorderUnavailableError):
        parse_permission_result(data, "microphone", "request")


def test_failed_settings_open_cannot_be_a_success():
    payload = {**result(action="open_settings"), "settings_opened": False}
    with pytest.raises(RecorderUnavailableError):
        parse_permission_result(json.dumps(payload).encode(), "microphone", "open_settings")


@pytest.mark.parametrize("knob,value", [("RESULT", "not-json"), ("EXIT", "3")])
def test_failure_invalidates_permissions_cache(knob: str, value: str, monkeypatch):
    ctl = RecordingController(FAKE_RECORDER)
    assert ctl.permissions() == GRANTED
    monkeypatch.setenv("FAKE_RECORDER_CHECK", json.dumps(DENIED))
    monkeypatch.setenv("FAKE_RECORDER_PERMISSION_" + knob, value)
    with pytest.raises(RecorderUnavailableError):
        ctl.configure_permission("microphone", "request")
    assert not ctl.permission_setup_in_progress
    assert ctl.permissions() == DENIED


def test_missing_binary_and_invalid_arguments(tmp_path: Path):
    ctl = RecordingController(tmp_path / "missing")
    with pytest.raises(RecorderUnavailableError):
        ctl.configure_permission("microphone", "request")
    assert not ctl.permission_setup_in_progress
    for permission, action in [("camera", "request"), ("microphone", "http://example.test")]:
        with pytest.raises(InvalidArgumentError):
            ctl.configure_permission(permission, action)


def test_spawn_failure_is_unavailable_and_clears_busy(monkeypatch):
    import narumi_server.permission_setup as setup

    def fail_spawn(*_args, **_kwargs):
        raise OSError(13, "fake launch denied")

    ctl = RecordingController(FAKE_RECORDER)
    monkeypatch.setattr(setup.subprocess, "Popen", fail_spawn)
    with pytest.raises(RecorderUnavailableError, match="cannot launch"):
        ctl.configure_permission("microphone", "request")
    assert not ctl.permission_setup_in_progress


@pytest.mark.parametrize("ignore_term", [False, True])
def test_timeout_reaps_only_owned_helper(ignore_term: bool, tmp_path: Path, monkeypatch):
    import narumi_server.permission_setup as setup

    marker = tmp_path / "permission.pid"
    monkeypatch.setenv("FAKE_RECORDER_PERMISSION_MARKER", str(marker))
    monkeypatch.setenv("FAKE_RECORDER_PERMISSION_DELAY", "5")
    if ignore_term:
        monkeypatch.setenv("FAKE_RECORDER_PERMISSION_IGNORE_TERM", "1")
    monkeypatch.setattr(setup, "PERMISSION_EXIT_GRACE", 0.15)
    ctl = RecordingController(FAKE_RECORDER, permission_timeout=0.4)
    assert PERMISSION_ACTION_TIMEOUT == 120.0
    with pytest.raises(RecorderUnavailableError, match="timed out"):
        ctl.configure_permission("microphone", "request")
    pid = wait_marker(marker)
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    assert not ctl.permission_setup_in_progress
    monkeypatch.delenv("FAKE_RECORDER_PERMISSION_DELAY")
    assert ctl.configure_permission("microphone", "request")["permissions"] == GRANTED


def test_permission_wait_blocks_start_but_not_status(tmp_path: Path, monkeypatch):
    marker = tmp_path / "permission.pid"
    monkeypatch.setenv("FAKE_RECORDER_PERMISSION_MARKER", str(marker))
    monkeypatch.setenv("FAKE_RECORDER_PERMISSION_DELAY", "0.6")
    ctl = RecordingController(FAKE_RECORDER)
    assert ctl.permissions() == GRANTED
    bundle = Bundle.create(tmp_path / "meetings", meeting_name="fake race")
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(ctl.configure_permission, "microphone", "request")
        wait_marker(marker)
        assert ctl.permission_setup_in_progress
        with pytest.raises(BusyError):
            ctl.start(bundle)
        with pytest.raises(BusyError):
            ctl.configure_permission("screen_recording", "request")
        monkeypatch.setattr(
            recording, "_run_check", lambda _p: pytest.fail("must use cache while busy")
        )
        assert ctl.permissions(max_age=0) == GRANTED
        assert not ctl.is_active and not ctl.process_alive
        assert not list(bundle.dir("tracks").iterdir())
        pending.result(timeout=5)


def test_start_wait_and_active_recording_block_permission(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("FAKE_RECORDER_START_DELAY", "0.5")
    ctl = RecordingController(FAKE_RECORDER)
    bundle = Bundle.create(tmp_path / "meetings", meeting_name="fake start")
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(ctl.start, bundle)
        deadline = time.monotonic() + 5
        while not ctl.process_alive and time.monotonic() < deadline:
            time.sleep(0.01)
        try:
            assert ctl.process_alive and not ctl.is_active
            with pytest.raises(BusyError):
                ctl.configure_permission("microphone", "request")
            pending.result(timeout=5)
            with pytest.raises(BusyError):
                ctl.configure_permission("screen_recording", "open_settings")
        finally:
            ctl.abort()


def test_late_check_cannot_write_stale_grant(monkeypatch):
    ctl = RecordingController(FAKE_RECORDER)
    entered, release = threading.Event(), threading.Event()
    calls = 0

    def delayed_check(_path: Path):
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            assert release.wait(5)
            return GRANTED
        return DENIED

    monkeypatch.setattr(recording, "_run_check", delayed_check)
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(ctl.permissions)
        assert entered.wait(5)
        try:
            ctl.configure_permission("microphone", "request")
        finally:
            release.set()
        assert pending.result(timeout=5) == DENIED
    assert ctl.permissions() == DENIED and calls == 2


def test_abort_reaps_permission_helper_and_prevents_shutdown_spawn(tmp_path: Path, monkeypatch):
    marker = tmp_path / "permission.pid"
    monkeypatch.setenv("FAKE_RECORDER_PERMISSION_MARKER", str(marker))
    monkeypatch.setenv("FAKE_RECORDER_PERMISSION_DELAY", "10")
    ctl = RecordingController(FAKE_RECORDER)
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(ctl.configure_permission, "microphone", "request")
        pid = wait_marker(marker)
        ctl.abort()
        with pytest.raises(RecorderUnavailableError):
            pending.result(timeout=5)
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    assert not ctl.permission_setup_in_progress
    with pytest.raises(RecorderUnavailableError, match="shutting down"):
        ctl.configure_permission("microphone", "request")
