"""``narumi-recorder`` subprocess control.

Wire protocol (JSON Lines on the recorder's stdout, one object per line; this is the contract the
Swift recorder in ``app/`` must match — see ``app/Sources/NarumiRecorderKit/RecorderEvents.swift``):

* ``{"event": "started", "started_at": <RFC3339 UTC>, "tracks": {<track>: <file name>}}``
  — emitted once capture runs; file names are relative to ``--output`` (``tracks/`` of the bundle)
* ``{"event": "stopped", "stopped_at": <RFC3339 UTC>, "duration_sec": <number>,
  "tracks": {<track>: {"path": <file name>, "bytes": <int>, "duration_sec": <number>}}}``
  — emitted after finalization; the process then exits 0
* ``{"event": "error", "code": <recorder code>, "message": <str>}`` — the process exits non-zero.
  Before ``stopped`` it is fatal (no usable tracks). After ``stopped`` it reports a capture
  failure (display gone, stream stopped by the system, …) whose audio tracks were nevertheless
  finalized: the recording is usable and the error is kept as provenance
  (:attr:`StoppedEvent.error`). Codes ``permission_denied`` / ``no_display`` /
  ``capture_failed`` / ``writer_failed`` map to ``recorder_unavailable``, ``invalid_argument`` to
  ``invalid_argument``, anything else to ``internal``.
* ``{"event": "log", "message": <str>}`` — informational, forwarded to the server log

``<recorder> check`` prints ``{"screen_recording": <status>, "microphone": <status>}`` with
``granted`` / ``denied`` / ``unknown`` (macOS reports screen recording as ``denied`` until it was
granted once; the first ``record`` run triggers the prompt).

The controller launches ``<recorder> record --output <bundle>/tracks``, stops with SIGINT (the
recorder also accepts a ``stop`` line on stdin) and never hard-codes track file names: they come
from the events.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any

from narumi.bundle import Bundle
from narumi.config import ENV_RECORDER, repo_root
from narumi.errors import (
    BusyError,
    ErrorCode,
    InvalidArgumentError,
    NarumiError,
    NotFoundError,
    RecorderUnavailableError,
)

logger = logging.getLogger(__name__)

DEFAULT_START_TIMEOUT = 20.0
DEFAULT_STOP_TIMEOUT = 30.0
EXIT_GRACE = 10.0
READER_JOIN_GRACE = 2.0
CHECK_TIMEOUT = 10.0
CHECK_CACHE_SECONDS = 5.0
STDERR_TAIL_CHARS = 2000
RECORDER_STDERR_LOG = "recorder.stderr.log"
PERMISSION_KEYS: tuple[str, ...] = ("screen_recording", "microphone")
PERMISSION_STATUSES: frozenset[str] = frozenset({"granted", "denied", "unknown"})

RECORDER_ERROR_CODES: dict[str, ErrorCode] = {
    "permission_denied": ErrorCode.RECORDER_UNAVAILABLE,
    "no_display": ErrorCode.RECORDER_UNAVAILABLE,
    "capture_failed": ErrorCode.RECORDER_UNAVAILABLE,
    "writer_failed": ErrorCode.RECORDER_UNAVAILABLE,
    "invalid_argument": ErrorCode.INVALID_ARGUMENT,
}


# ---------------------------------------------------------------------------- events
@dataclass(frozen=True)
class StartedEvent:
    """Parsed ``started`` event."""

    started_at: str
    tracks: dict[str, str]
    """Track name → file name relative to the recorder output directory."""
    raw: dict[str, Any] = field(default_factory=dict, compare=False)

    @classmethod
    def parse(cls, event: Mapping[str, Any]) -> StartedEvent:
        started_at = event.get("started_at")
        tracks = event.get("tracks")
        if not isinstance(started_at, str) or not started_at:
            raise _malformed("started", "missing started_at", event)
        if not isinstance(tracks, dict) or not tracks:
            raise _malformed("started", "missing tracks", event)
        parsed: dict[str, str] = {}
        for name, value in tracks.items():
            if not isinstance(name, str) or not isinstance(value, str) or not value:
                raise _malformed("started", f"bad track entry {name!r}", event)
            parsed[name] = value
        return cls(started_at=started_at, tracks=parsed, raw=dict(event))


@dataclass(frozen=True)
class TrackSummary:
    path: str
    bytes: int | None = None
    duration_sec: float | None = None


@dataclass(frozen=True)
class StoppedEvent:
    """Parsed ``stopped`` event."""

    stopped_at: str
    duration_sec: float
    tracks: dict[str, TrackSummary]
    raw: dict[str, Any] = field(default_factory=dict, compare=False)
    error: dict[str, Any] | None = field(default=None, compare=False)
    """Structured error (``{"code", "message", "details"}``) when the recorder reported a capture
    failure *after* finalizing the tracks; ``None`` for a clean stop."""

    @classmethod
    def parse(cls, event: Mapping[str, Any]) -> StoppedEvent:
        stopped_at = event.get("stopped_at")
        duration = event.get("duration_sec")
        tracks = event.get("tracks")
        if not isinstance(stopped_at, str) or not stopped_at:
            raise _malformed("stopped", "missing stopped_at", event)
        if not isinstance(duration, int | float) or isinstance(duration, bool) or duration < 0:
            raise _malformed("stopped", "missing or negative duration_sec", event)
        if not isinstance(tracks, dict):
            raise _malformed("stopped", "missing tracks", event)
        parsed: dict[str, TrackSummary] = {}
        for name, value in tracks.items():
            if isinstance(value, str) and value:
                parsed[name] = TrackSummary(path=value)
                continue
            if not isinstance(value, dict) or not isinstance(value.get("path"), str):
                raise _malformed("stopped", f"bad track entry {name!r}", event)
            size = value.get("bytes")
            dur = value.get("duration_sec")
            parsed[name] = TrackSummary(
                path=value["path"],
                bytes=size if isinstance(size, int) and not isinstance(size, bool) else None,
                duration_sec=float(dur) if isinstance(dur, int | float) else None,
            )
        return cls(
            stopped_at=stopped_at, duration_sec=float(duration), tracks=parsed, raw=dict(event)
        )


def _malformed(name: str, why: str, event: Mapping[str, Any]) -> RecorderUnavailableError:
    return RecorderUnavailableError(
        f"recorder emitted a malformed {name!r} event: {why}",
        details={"event": dict(event)},
    )


def recorder_error(event: Mapping[str, Any]) -> NarumiError:
    """Map a recorder ``error`` event to a structured :class:`NarumiError`."""
    code = str(event.get("code") or "unknown")
    message = str(event.get("message") or "recorder reported an error")
    mapped = RECORDER_ERROR_CODES.get(code, ErrorCode.INTERNAL)
    details = {"recorder_code": code, "recorder_message": message}
    if mapped == ErrorCode.RECORDER_UNAVAILABLE:
        return RecorderUnavailableError(f"recorder error ({code}): {message}", details=details)
    if mapped == ErrorCode.INVALID_ARGUMENT:
        return InvalidArgumentError(f"recorder rejected its arguments: {message}", details=details)
    return NarumiError(f"recorder error ({code}): {message}", code=mapped, details=details)


# ---------------------------------------------------------------------------- binary lookup
def recorder_candidates(env: Mapping[str, str] | None = None) -> list[Path]:
    """Paths tried in order: ``$NARUMI_RECORDER`` alone when set, else the swift build outputs."""
    environ = os.environ if env is None else env
    override = environ.get(ENV_RECORDER)
    if override:
        return [Path(override).expanduser()]
    root = repo_root()
    return [
        root / "app" / ".build" / "release" / "narumi-recorder",
        root / "app" / ".build" / "debug" / "narumi-recorder",
    ]


def resolve_recorder_path(env: Mapping[str, str] | None = None) -> Path | None:
    """First usable candidate (executable file, or a ``.py`` script run with this interpreter)."""
    for candidate in recorder_candidates(env):
        if candidate.is_file() and (candidate.suffix == ".py" or os.access(candidate, os.X_OK)):
            return candidate
    return None


def recorder_command(path: Path) -> list[str]:
    """argv prefix for ``path`` (``.py`` scripts run under the current interpreter)."""
    if path.suffix == ".py":
        return [sys.executable, str(path)]
    return [str(path)]


# ---------------------------------------------------------------------------- controller
class RecordingController:
    """Owns at most one ``narumi-recorder`` process (同時録画は 1 本)."""

    def __init__(
        self,
        recorder_path: Path | None = None,
        *,
        start_timeout: float = DEFAULT_START_TIMEOUT,
        stop_timeout: float = DEFAULT_STOP_TIMEOUT,
        extra_args: Sequence[str] = (),
    ) -> None:
        self._explicit_path = Path(recorder_path) if recorder_path is not None else None
        self._start_timeout = start_timeout
        self._stop_timeout = stop_timeout
        self._extra_args = tuple(extra_args)
        self._lock = threading.RLock()
        self._proc: subprocess.Popen[bytes] | None = None
        self._events: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self._reader: threading.Thread | None = None
        self._stderr: IO[bytes] | None = None
        self._stderr_path: Path | None = None
        self._active_meeting_id: str | None = None
        self._active_bundle_path: Path | None = None
        self._permissions_cache: tuple[float, Path, dict[str, str] | None] | None = None

    # ------------------------------------------------------------------ availability
    @property
    def recorder_path(self) -> Path | None:
        if self._explicit_path is not None:
            path = self._explicit_path
            usable = path.is_file() and (path.suffix == ".py" or os.access(path, os.X_OK))
            return path if usable else None
        return resolve_recorder_path()

    def candidates(self) -> list[Path]:
        if self._explicit_path is not None:
            return [self._explicit_path]
        return recorder_candidates()

    def available(self) -> bool:
        """Whether ``start_recording`` can be attempted: binary found, microphone not denied.

        ``<recorder> check`` is run (cached for a few seconds). Microphone ``denied`` is
        definitive (macOS never prompts again) and makes this false; ``unknown`` does not,
        because the first recording triggers the prompt. Screen recording is deliberately not
        consulted: CoreGraphics reports it as ``denied`` until it was granted once, so it cannot
        distinguish "never asked" from "refused" — the recorder reports ``permission_denied`` at
        start when it really is refused. A failing / hanging check counts as not available: the
        recorder would not work either.
        """
        if self.recorder_path is None:
            return False
        permissions = self.permissions()
        return permissions is not None and permissions.get("microphone") != "denied"

    def permissions(self, *, max_age: float = CHECK_CACHE_SECONDS) -> dict[str, str] | None:
        """Permission report of ``<recorder> check`` (``None`` when unavailable or malformed)."""
        path = self.recorder_path
        if path is None:
            return None
        cached = self._permissions_cache
        now = time.monotonic()
        if cached is not None and cached[1] == path and now - cached[0] <= max_age:
            return None if cached[2] is None else dict(cached[2])
        report = _run_check(path)
        self._permissions_cache = (now, path, report)
        return None if report is None else dict(report)

    def require_available(self) -> Path:
        path = self.recorder_path
        if path is None:
            looked = [str(p) for p in self.candidates()]
            raise RecorderUnavailableError(
                "narumi-recorder binary not found; build it with `cd app && swift build -c release`"
                f" or set {ENV_RECORDER} (looked in: {', '.join(looked)})",
                details={"candidates": looked, "env": ENV_RECORDER},
            )
        return path

    # ------------------------------------------------------------------ state
    @property
    def active_meeting_id(self) -> str | None:
        return self._active_meeting_id

    @property
    def active_bundle_path(self) -> Path | None:
        return self._active_bundle_path

    @property
    def is_active(self) -> bool:
        return self._active_meeting_id is not None

    @property
    def process_alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    # ------------------------------------------------------------------ start / stop
    def start(self, bundle: Bundle, *, no_video: bool = False) -> StartedEvent:
        """Launch the recorder for ``bundle`` and wait for its ``started`` event."""
        with self._lock:
            if self._active_meeting_id is not None:
                raise BusyError(
                    "a recording is already running",
                    details={"meeting_id": self._active_meeting_id},
                )
            path = self.require_available()
            out_dir = bundle.dir("tracks")
            self._stderr_path = bundle.dir("logs") / RECORDER_STDERR_LOG
            args = [*recorder_command(path), "record", "--output", str(out_dir)]
            if no_video:
                args.append("--no-video")
            args.extend(self._extra_args)
            stderr = self._stderr_path.open("ab")
            try:
                proc = subprocess.Popen(  # noqa: S603 - argv list, no shell
                    args,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=stderr,
                    cwd=str(bundle.path),
                )
            except OSError as exc:
                stderr.close()
                raise RecorderUnavailableError(
                    f"cannot launch narumi-recorder ({path}): {exc}",
                    details={"recorder": str(path), "errno": exc.errno},
                ) from exc
            self._proc = proc
            self._stderr = stderr
            self._events = queue.Queue()
            assert proc.stdout is not None
            self._reader = threading.Thread(
                target=_pump_events,
                args=(proc.stdout, self._events),
                name="narumi-recorder-stdout",
                daemon=True,
            )
            self._reader.start()
            logger.info("recorder started: %s (pid %s)", " ".join(args), proc.pid)
            try:
                event = self._wait_for("started", timeout=self._start_timeout, on_timeout="kill")
                started = StartedEvent.parse(event)
            except BaseException:
                self._terminate(proc)
                self._cleanup()
                raise
            self._permissions_cache = None  # a granted prompt changes the report
            self._active_meeting_id = bundle.meeting_id
            self._active_bundle_path = bundle.path
            return started

    def stop(self) -> StoppedEvent:
        """Send SIGINT, wait for the ``stopped`` event and reap the process.

        Always clears the active recording, even on failure: the caller marks the meeting
        ``failed`` and a new recording may start.
        """
        with self._lock:
            if self._active_meeting_id is None or self._proc is None:
                raise NotFoundError("no recording is running")
            proc = self._proc
            try:
                if proc.poll() is None:
                    _send_stop(proc)
                event = self._wait_for("stopped", timeout=self._stop_timeout, on_timeout="kill")
                stopped = StoppedEvent.parse(event)
                try:
                    proc.wait(timeout=EXIT_GRACE)
                except subprocess.TimeoutExpired:
                    logger.warning("recorder still alive after 'stopped'; killing %s", proc.pid)
                    proc.kill()
                    proc.wait()
                error = self._trailing_error()
                if error is not None:
                    logger.warning(
                        "recorder finalized the tracks but reported %s: %s (exit %s)",
                        error["code"],
                        error["message"],
                        proc.returncode,
                    )
                    stopped = StoppedEvent(
                        stopped_at=stopped.stopped_at,
                        duration_sec=stopped.duration_sec,
                        tracks=stopped.tracks,
                        raw=stopped.raw,
                        error=error,
                    )
                logger.info("recorder stopped (exit %s)", proc.returncode)
                return stopped
            except BaseException:
                self._terminate(proc)
                raise
            finally:
                self._cleanup()

    def abort(self) -> None:
        """Stop a running recorder without reading its events (server shutdown).

        The recorder finalizes its files on SIGINT / stdin EOF, so it gets both and up to the
        stop timeout to exit on its own; only then is it killed. Killing first would leave
        ``screen.mp4`` / ``mic.m4a`` / ``system.m4a`` without their container index.
        """
        with self._lock:
            proc = self._proc
            if proc is not None and proc.poll() is None:
                _send_stop(proc)
                if proc.stdin is not None:
                    try:
                        proc.stdin.close()  # EOF: the recorder's parent-loss finalize path
                    except OSError:  # pragma: no cover
                        pass
                try:
                    proc.wait(timeout=self._stop_timeout)
                    logger.info("recorder exited on abort (exit %s)", proc.returncode)
                except subprocess.TimeoutExpired:
                    logger.warning("recorder did not exit within %gs; killing", self._stop_timeout)
                    self._terminate(proc)
            self._cleanup()

    # ------------------------------------------------------------------ internals
    def _wait_for(self, name: str, *, timeout: float, on_timeout: str) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                if name == "started":
                    # The usual cause is a blocking macOS permission dialog (first run) or a
                    # recorder that cannot reach ScreenCaptureKit: not an internal server fault.
                    raise RecorderUnavailableError(
                        f"recorder did not emit 'started' within {timeout:g}s; a pending screen "
                        "recording / microphone permission dialog may be blocking it — answer "
                        "it (or grant access in System Settings) and start again",
                        details={
                            "event": name,
                            "timeout_sec": timeout,
                            "action": on_timeout,
                            "permissions": self.permissions(max_age=0.0),
                        },
                    )
                raise NarumiError(
                    f"recorder did not emit {name!r} within {timeout:g}s",
                    code=ErrorCode.INTERNAL,
                    details={"event": name, "timeout_sec": timeout, "action": on_timeout},
                )
            try:
                item = self._events.get(timeout=min(remaining, 0.5))
            except queue.Empty:
                continue
            if item is None:  # EOF: the process is gone
                proc = self._proc
                code = proc.wait(timeout=EXIT_GRACE) if proc is not None else None
                raise RecorderUnavailableError(
                    f"recorder exited (code {code}) before emitting {name!r}",
                    details={"returncode": code, "stderr_tail": self._stderr_tail()},
                )
            kind = item.get("event")
            if kind == name:
                return item
            if kind == "error":
                raise recorder_error(item)
            if kind == "log":
                logger.info("recorder: %s", item.get("message", ""))
            else:
                logger.debug("recorder: ignoring event %r while waiting for %r", kind, name)

    def _trailing_error(self) -> dict[str, Any] | None:
        """An ``error`` event emitted after ``stopped`` (the process has exited by now)."""
        reader = self._reader
        if reader is not None:
            reader.join(timeout=READER_JOIN_GRACE)  # stdout EOF → every line is queued
        error: dict[str, Any] | None = None
        while True:
            try:
                item = self._events.get_nowait()
            except queue.Empty:
                return error
            if item is None:
                return error
            kind = item.get("event")
            if kind == "error":
                error = recorder_error(item).to_payload()["error"]
            elif kind == "log":
                logger.info("recorder: %s", item.get("message", ""))

    def _stderr_tail(self) -> str:
        path = self._stderr_path
        if path is None or not path.exists():
            return ""
        try:
            data = path.read_bytes()
        except OSError:
            return ""
        return data[-STDERR_TAIL_CHARS:].decode("utf-8", "replace")

    @staticmethod
    def _terminate(proc: subprocess.Popen[bytes]) -> None:
        if proc.poll() is not None:
            return
        try:
            proc.kill()
            proc.wait(timeout=EXIT_GRACE)
        except (OSError, subprocess.TimeoutExpired):  # pragma: no cover - best effort
            logger.warning("could not kill recorder pid %s", proc.pid)

    def _cleanup(self) -> None:
        proc = self._proc
        if proc is not None:
            for stream in (proc.stdin, proc.stdout):
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:  # pragma: no cover
                        pass
        if self._stderr is not None:
            self._stderr.close()
        self._proc = None
        self._stderr = None
        self._reader = None
        self._active_meeting_id = None
        self._active_bundle_path = None


def _run_check(path: Path) -> dict[str, str] | None:
    """Run ``<recorder> check`` and parse its permission report; ``None`` on any failure."""
    args = [*recorder_command(path), "check"]
    try:
        completed = subprocess.run(  # noqa: S603 - argv list, no shell
            args, capture_output=True, timeout=CHECK_TIMEOUT, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("recorder check failed to run (%s): %s", path, exc)
        return None
    if completed.returncode != 0:
        logger.warning(
            "recorder check exited %s: %s",
            completed.returncode,
            completed.stderr.decode("utf-8", "replace")[-STDERR_TAIL_CHARS:],
        )
        return None
    text = completed.stdout.decode("utf-8", "replace").strip()
    try:
        report = json.loads(text.splitlines()[-1]) if text else None
    except json.JSONDecodeError:
        report = None
    if not isinstance(report, dict) or not all(
        report.get(key) in PERMISSION_STATUSES for key in PERMISSION_KEYS
    ):
        logger.warning("recorder check printed an unexpected report: %s", text[:200])
        return None
    return {key: str(report[key]) for key in PERMISSION_KEYS}


def _send_stop(proc: subprocess.Popen[bytes]) -> None:
    try:
        proc.send_signal(signal.SIGINT)
    except OSError as exc:  # pragma: no cover - process vanished between poll() and signal
        logger.warning("SIGINT to recorder pid %s failed: %s", proc.pid, exc)
    if proc.stdin is not None:
        try:
            proc.stdin.write(b"stop\n")
            proc.stdin.flush()
        except (OSError, ValueError):
            pass


def _pump_events(stream: IO[bytes], events: queue.Queue[dict[str, Any] | None]) -> None:
    try:
        for raw in iter(stream.readline, b""):
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                logger.warning("recorder: non-JSON stdout line: %s", line[:200])
                continue
            if isinstance(obj, dict):
                events.put(obj)
            else:
                logger.warning("recorder: unexpected stdout JSON: %s", line[:200])
    except (OSError, ValueError):  # stream closed under us
        pass
    finally:
        events.put(None)
