"""Unit tests for ``RecordingController`` against the fake recorder script."""

from __future__ import annotations

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
