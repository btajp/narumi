from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from narumi.errors import CancelledError, EngineUnavailableError
from narumi.playback._media import inspect_media
from narumi.playback._process import run_media_tool
from narumi.preprocess.ffmpeg import FfmpegError


def test_cancellation_terminates_and_reaps_ffmpeg(monkeypatch):
    class Process:
        terminated = False
        reaped = False

        def wait(self, timeout=None):
            if self.terminated:
                self.reaped = True
                return -15
            raise subprocess.TimeoutExpired("ffmpeg", timeout)

        def poll(self):
            return None

        def terminate(self):
            self.terminated = True

    process = Process()
    monkeypatch.setattr("narumi.playback._process.subprocess.Popen", lambda *a, **kw: process)
    checks = iter([False, False, True])
    with pytest.raises(CancelledError):
        run_media_tool(["ffmpeg", "-i", "input"], should_cancel=lambda: next(checks))
    assert process.terminated and process.reaped


def test_cancelled_child_that_ignores_terminate_is_killed(monkeypatch):
    class Process:
        killed = False
        terminated = False

        def wait(self, timeout=None):
            if self.killed:
                return -9
            raise subprocess.TimeoutExpired("ffmpeg", timeout)

        def poll(self):
            return None

        def terminate(self):
            self.terminated = True

        def kill(self):
            self.killed = True

    process = Process()
    monkeypatch.setattr("narumi.playback._process.subprocess.Popen", lambda *a, **kw: process)
    checks = iter([False, True])
    with pytest.raises(CancelledError):
        run_media_tool(["ffmpeg"], should_cancel=lambda: next(checks))
    assert process.terminated and process.killed


def test_unavailable_binary_is_typed(monkeypatch):
    def missing(*args, **kwargs):
        raise PermissionError("unexecutable")

    monkeypatch.setattr("narumi.playback._process.subprocess.Popen", missing)
    with pytest.raises(EngineUnavailableError):
        run_media_tool(["ffmpeg"])


def test_nonzero_exit_is_typed_and_log_tail_is_bounded(monkeypatch):
    class Process:
        def wait(self, timeout=None):
            return 1

    def failed(*args, **kwargs):
        kwargs["stderr"].write(b"x" * 5000 + b"corrupted media")
        return Process()

    monkeypatch.setattr("narumi.playback._process.subprocess.Popen", failed)
    with pytest.raises(FfmpegError) as caught:
        run_media_tool(["ffmpeg"])
    assert caught.value.details["stderr_tail"].endswith("corrupted media")
    assert len(caught.value.details["stderr_tail"]) <= 2000


@pytest.mark.parametrize("raw", [b"not JSON", b"{}", b'{"streams": []}', b"[]"])
def test_invalid_probe_response_is_typed(monkeypatch, raw):
    monkeypatch.setattr("narumi.playback._media.ffprobe_path", lambda: Path("ffprobe"))
    monkeypatch.setattr("narumi.playback._media.run_media_tool", lambda *a, **kw: raw)
    with pytest.raises(FfmpegError):
        inspect_media(Path("screen.mp4"))
