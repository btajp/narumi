"""Cancellable subprocess execution for local playback preparation."""

from __future__ import annotations

import subprocess
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

from narumi.errors import CancelledError, EngineUnavailableError
from narumi.preprocess.ffmpeg import STDERR_TAIL_CHARS, FfmpegError

CancelCheck = Callable[[], bool] | None


def check_cancelled(should_cancel: CancelCheck) -> None:
    if should_cancel is not None and should_cancel():
        raise CancelledError("recording playback preparation cancelled")


def run_media_tool(
    args: list[str],
    *,
    should_cancel: CancelCheck = None,
    timeout: float | None = None,
    capture_stderr: bool = False,
) -> bytes:
    """Return stdout (or measurement stderr), polling cancellation and reaping the child."""
    check_cancelled(should_cancel)
    tool = Path(args[0]).name
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        try:
            process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr)
        except OSError as exc:
            raise EngineUnavailableError(
                f"{tool} could not be executed: {exc}", details={"tool": tool}
            ) from exc
        started = time.monotonic()
        try:
            while True:
                check_cancelled(should_cancel)
                if timeout is not None and time.monotonic() - started > timeout:
                    raise FfmpegError(f"{tool} timed out after {timeout}s")
                try:
                    returncode = process.wait(timeout=0.1)
                    break
                except subprocess.TimeoutExpired:
                    continue
            check_cancelled(should_cancel)
        except BaseException:
            if process.poll() is None:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass  # The child exited between poll() and terminate().
                try:
                    process.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                    process.wait()
            raise
        if returncode != 0:
            stderr.seek(0, 2)
            stderr.seek(max(0, stderr.tell() - STDERR_TAIL_CHARS))
            tail = stderr.read().decode("utf-8", errors="replace").strip()
            raise FfmpegError(
                f"{tool} failed with exit code {returncode}",
                details={"tool": tool, "returncode": returncode, "stderr_tail": tail},
            )
        captured = stderr if capture_stderr else stdout
        captured.seek(0)
        return captured.read()
