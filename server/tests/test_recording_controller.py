"""Unit tests for ``RecordingController`` against the fake recorder script."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest
from conftest import FAKE_RECORDER
from narumi.bundle import Bundle
from narumi.errors import (
    BusyError,
    InvalidArgumentError,
    NarumiError,
    NotFoundError,
    RecorderUnavailableError,
)
from narumi_server.recording import (
    RecordingController,
    StartedEvent,
    StoppedEvent,
    recorder_candidates,
    recorder_command,
    recorder_error,
    resolve_recorder_path,
)


@pytest.fixture
def bundle(tmp_path: Path) -> Bundle:
    return Bundle.create(tmp_path / "meetings", meeting_name="ctl")


def test_resolve_recorder_path(tmp_path: Path):
    assert resolve_recorder_path({"NARUMI_RECORDER": str(FAKE_RECORDER)}) == FAKE_RECORDER
    assert resolve_recorder_path({"NARUMI_RECORDER": str(tmp_path / "nope")}) is None
    binary = tmp_path / "narumi-recorder"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    assert resolve_recorder_path({"NARUMI_RECORDER": str(binary)}) is None  # not executable
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    assert resolve_recorder_path({"NARUMI_RECORDER": str(binary)}) == binary
    candidates = recorder_candidates({})
    assert [p.name for p in candidates] == ["narumi-recorder", "narumi-recorder"]
    assert candidates[0].parts[-3:-1] == (".build", "release")
    assert candidates[1].parts[-3:-1] == (".build", "debug")
    assert recorder_command(FAKE_RECORDER)[-1] == str(FAKE_RECORDER)
    assert recorder_command(binary) == [str(binary)]


def test_event_parsing():
    started = StartedEvent.parse(
        {"event": "started", "started_at": "2026-08-27T03:05:00Z", "tracks": {"mic": "mic.m4a"}}
    )
    assert started.tracks == {"mic": "mic.m4a"}
    with pytest.raises(RecorderUnavailableError):
        StartedEvent.parse({"event": "started", "started_at": "x", "tracks": {}})
    with pytest.raises(RecorderUnavailableError):
        StartedEvent.parse({"event": "started", "tracks": {"mic": "mic.m4a"}})
    stopped = StoppedEvent.parse(
        {
            "event": "stopped",
            "stopped_at": "2026-08-27T04:00:00Z",
            "duration_sec": 12,
            "tracks": {
                "mic": {"path": "mic.m4a", "bytes": 10, "duration_sec": 11.5},
                "screen": "screen.mp4",
            },
        }
    )
    assert stopped.duration_sec == 12.0
    assert stopped.tracks["mic"].bytes == 10 and stopped.tracks["mic"].duration_sec == 11.5
    assert stopped.tracks["screen"].bytes is None
    with pytest.raises(RecorderUnavailableError):
        StoppedEvent.parse(
            {"event": "stopped", "stopped_at": "x", "duration_sec": -1, "tracks": {}}
        )
    with pytest.raises(RecorderUnavailableError):
        StoppedEvent.parse(
            {"event": "stopped", "stopped_at": "x", "duration_sec": 1, "tracks": {"a": 1}}
        )


def test_recorder_error_mapping():
    assert isinstance(
        recorder_error({"code": "permission_denied", "message": "m"}), RecorderUnavailableError
    )
    assert isinstance(
        recorder_error({"code": "invalid_argument", "message": "m"}), InvalidArgumentError
    )
    other = recorder_error({"code": "something_else", "message": "m"})
    assert type(other) is NarumiError and str(other.code) == "internal"
    assert other.details == {"recorder_code": "something_else", "recorder_message": "m"}


def test_start_stop_roundtrip(bundle: Bundle):
    ctl = RecordingController(FAKE_RECORDER)
    assert ctl.available()
    started = ctl.start(bundle)
    assert set(started.tracks) == {"screen", "mic", "system"}
    assert started.display["id"] == 1
    assert started.display["is_main"]
    assert ctl.is_active and ctl.active_meeting_id == bundle.meeting_id
    assert ctl.process_alive
    with pytest.raises(BusyError):
        ctl.start(bundle)
    stopped = ctl.stop()
    assert stopped.tracks["mic"].path == "mic.wav"
    assert stopped.tracks["mic"].bytes == (bundle.path / "tracks" / "mic.wav").stat().st_size
    assert (bundle.path / "tracks" / "recorder.json").is_file()
    assert not ctl.is_active
    with pytest.raises(NotFoundError):
        ctl.stop()
    # no-video: the screen track is absent from the events
    started = ctl.start(bundle, no_video=True)
    assert set(started.tracks) == {"mic", "system"}
    assert set(ctl.stop().tracks) == {"mic", "system"}


def test_start_selects_requested_display(bundle: Bundle):
    ctl = RecordingController(FAKE_RECORDER)
    try:
        started = ctl.start(bundle, display_id=2)
        assert started.display["id"] == 2
        assert not started.display["is_main"]
    finally:
        ctl.abort()


@pytest.mark.parametrize("display_id", [0, -1, 2**32, True, 1.5, "2"])
def test_start_rejects_invalid_display_id_without_launching(bundle: Bundle, display_id):
    ctl = RecordingController(FAKE_RECORDER)
    with pytest.raises(InvalidArgumentError):
        ctl.start(bundle, display_id=display_id)
    assert not ctl.process_alive


def test_start_disconnected_display_does_not_fall_back(bundle: Bundle):
    ctl = RecordingController(FAKE_RECORDER)
    with pytest.raises(RecorderUnavailableError) as exc:
        ctl.start(bundle, display_id=9)
    assert exc.value.details["recorder_code"] == "no_display"
    assert not ctl.is_active


def test_list_displays_is_recording_free():
    ctl = RecordingController(FAKE_RECORDER)
    displays = ctl.list_displays()
    assert [display["id"] for display in displays] == [2, 1]
    assert [display["id"] for display in displays if display["is_main"]] == [1]
    assert not ctl.is_active and not ctl.process_alive


@pytest.mark.parametrize(
    "report",
    ["invalid", "{}", "[null]", '[{"id":true}]', "[1]"],
)
def test_list_displays_rejects_malformed_report(monkeypatch, report):
    monkeypatch.setenv("FAKE_RECORDER_DISPLAYS", report)
    with pytest.raises(RecorderUnavailableError):
        RecordingController(FAKE_RECORDER).list_displays()


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("id", 0),
        ("id", 2**32),
        ("id", True),
        ("name", ""),
        ("width", -1),
        ("height", True),
        ("is_main", "true"),
    ],
)
def test_display_fields_are_validated_in_list_and_started(monkeypatch, key, value):
    display = {"id": 1, "name": "Main", "width": 1920, "height": 1080, "is_main": True}
    display[key] = value
    monkeypatch.setenv("FAKE_RECORDER_DISPLAYS", json.dumps([display]))
    with pytest.raises(RecorderUnavailableError):
        RecordingController(FAKE_RECORDER).list_displays()
    with pytest.raises(RecorderUnavailableError):
        StartedEvent.parse({"started_at": "x", "tracks": {"mic": "mic.wav"}, "display": display})


@pytest.mark.parametrize("same_id", [True, False])
def test_list_displays_rejects_duplicate_ids_or_main_flags(monkeypatch, same_id):
    display = {"id": 1, "name": "Main", "width": 1920, "height": 1080, "is_main": True}
    other = {**display, "id": 1 if same_id else 2}
    monkeypatch.setenv("FAKE_RECORDER_DISPLAYS", json.dumps([display, other]))
    with pytest.raises(RecorderUnavailableError):
        RecordingController(FAKE_RECORDER).list_displays()


def test_list_displays_maps_recorder_error(monkeypatch):
    monkeypatch.setenv("FAKE_RECORDER_DISPLAYS_EXIT", "1")
    monkeypatch.setenv(
        "FAKE_RECORDER_DISPLAYS",
        json.dumps({"event": "error", "code": "permission_denied", "message": "screen denied"}),
    )
    with pytest.raises(RecorderUnavailableError) as exc:
        RecordingController(FAKE_RECORDER).list_displays()
    assert exc.value.details["recorder_code"] == "permission_denied"


def test_list_displays_timeout_is_bounded(monkeypatch):
    monkeypatch.setenv("FAKE_RECORDER_DISPLAYS_DELAY", "5")
    monkeypatch.setattr("narumi_server.recording.CHECK_TIMEOUT", 0.1)
    with pytest.raises(RecorderUnavailableError):
        RecordingController(FAKE_RECORDER).list_displays()


def test_list_displays_accepts_empty_and_rejects_missing_binary(monkeypatch, tmp_path):
    monkeypatch.setenv("FAKE_RECORDER_DISPLAYS", "[]")
    assert RecordingController(FAKE_RECORDER).list_displays() == []
    with pytest.raises(RecorderUnavailableError):
        RecordingController(tmp_path / "missing").list_displays()


def test_missing_binary(bundle: Bundle, tmp_path: Path):
    ctl = RecordingController(tmp_path / "missing")
    assert not ctl.available()
    with pytest.raises(RecorderUnavailableError) as exc:
        ctl.start(bundle)
    assert exc.value.details["candidates"] == [str(tmp_path / "missing")]


def test_error_event_on_start(bundle: Bundle, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("FAKE_RECORDER_FAIL", "no_display")
    ctl = RecordingController(FAKE_RECORDER)
    with pytest.raises(RecorderUnavailableError) as exc:
        ctl.start(bundle)
    assert exc.value.details["recorder_code"] == "no_display"
    assert not ctl.is_active and not ctl.process_alive


def test_start_timeout(bundle: Bundle, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("FAKE_RECORDER_START_DELAY", "5")
    ctl = RecordingController(FAKE_RECORDER, start_timeout=0.5)
    with pytest.raises(RecorderUnavailableError) as exc:
        ctl.start(bundle)
    assert "permission" in exc.value.message
    assert exc.value.details["event"] == "started"
    assert exc.value.details["permissions"] == {
        "screen_recording": "granted",
        "microphone": "granted",
    }
    assert not ctl.process_alive  # killed


def test_permissions_and_availability(bundle: Bundle, tmp_path: Path, monkeypatch):
    ctl = RecordingController(FAKE_RECORDER)
    assert ctl.permissions() == {"screen_recording": "granted", "microphone": "granted"}
    assert ctl.available()
    monkeypatch.setenv(
        "FAKE_RECORDER_CHECK", '{"screen_recording": "denied", "microphone": "unknown"}'
    )
    assert ctl.permissions() == {"screen_recording": "granted", "microphone": "granted"}  # cached
    assert ctl.permissions(max_age=0.0) == {"screen_recording": "denied", "microphone": "unknown"}
    assert ctl.available()  # screen "denied" also means "never asked"; unknown mic prompts
    monkeypatch.setenv(
        "FAKE_RECORDER_CHECK", '{"screen_recording": "granted", "microphone": "denied"}'
    )
    assert ctl.permissions(max_age=0.0)["microphone"] == "denied"
    assert not ctl.available()
    monkeypatch.setenv("FAKE_RECORDER_CHECK", "not json")
    assert ctl.permissions(max_age=0.0) is None
    assert not ctl.available()
    monkeypatch.setenv(
        "FAKE_RECORDER_CHECK", '{"screen_recording": "maybe", "microphone": "granted"}'
    )
    assert ctl.permissions(max_age=0.0) is None
    assert RecordingController(tmp_path / "missing").permissions() is None


def test_error_after_stopped_keeps_the_tracks(bundle: Bundle, monkeypatch: pytest.MonkeyPatch):
    """A capture failure reported after finalization is provenance, not a failed recording."""
    monkeypatch.setenv("FAKE_RECORDER_ERROR_AFTER_STOP", "capture_failed")
    ctl = RecordingController(FAKE_RECORDER)
    ctl.start(bundle)
    stopped = ctl.stop()
    assert stopped.tracks["mic"].bytes and stopped.tracks["system"].bytes
    assert stopped.error is not None
    assert stopped.error["code"] == "recorder_unavailable"
    assert stopped.error["details"]["recorder_code"] == "capture_failed"
    assert not ctl.is_active and not ctl.process_alive


def test_stop_timeout_kills(bundle: Bundle, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("FAKE_RECORDER_STOP_DELAY", "5")
    ctl = RecordingController(FAKE_RECORDER, stop_timeout=0.5)
    ctl.start(bundle)
    with pytest.raises(NarumiError) as exc:
        ctl.stop()
    assert exc.value.details["event"] == "stopped"
    assert not ctl.is_active and not ctl.process_alive


def test_crash_on_stop(bundle: Bundle, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("FAKE_RECORDER_CRASH_ON_STOP", "1")
    ctl = RecordingController(FAKE_RECORDER)
    ctl.start(bundle)
    with pytest.raises(RecorderUnavailableError) as exc:
        ctl.stop()
    assert exc.value.details["returncode"] == 3
    assert not ctl.is_active


def test_abort_lets_the_recorder_finalize(bundle: Bundle):
    ctl = RecordingController(FAKE_RECORDER)
    ctl.start(bundle)
    proc = ctl._proc  # noqa: SLF001
    assert proc is not None
    ctl.abort()
    assert not ctl.is_active
    with pytest.raises(ProcessLookupError):
        os.kill(proc.pid, 0)
    assert proc.returncode == 0  # exited on its own after SIGINT / stdin EOF, not SIGKILLed
    assert (bundle.path / "tracks" / "recorder.json").is_file()  # finalized


def test_abort_kills_a_stuck_recorder(bundle: Bundle, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("FAKE_RECORDER_STOP_DELAY", "30")
    ctl = RecordingController(FAKE_RECORDER, stop_timeout=0.5)
    ctl.start(bundle)
    proc = ctl._proc  # noqa: SLF001
    assert proc is not None
    ctl.abort()
    assert not ctl.is_active and proc.returncode is not None and proc.returncode != 0
