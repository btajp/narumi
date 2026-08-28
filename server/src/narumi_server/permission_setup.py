"""Bounded, recording-free permission actions on the app-owned recorder helper."""

from __future__ import annotations

import json
import subprocess
import threading
from collections.abc import Sequence
from typing import Any

from narumi.errors import BusyError, InvalidArgumentError, RecorderUnavailableError

PERMISSION_ACTION_TIMEOUT = 120.0
PERMISSION_EXIT_GRACE = 2.0
PERMISSIONS = frozenset({"microphone", "screen_recording"})
PERMISSION_STATUSES = frozenset({"granted", "denied", "unknown"})
PERMISSION_COMMANDS = {
    "request": "request-permission",
    "open_settings": "open-permission-settings",
}
RESULT_KEYS = frozenset({"permission", "action", "permissions", "settings_opened"})


def validate_action(permission: str, action: str) -> None:
    if not isinstance(permission, str) or permission not in PERMISSIONS:
        raise InvalidArgumentError("unknown recording permission")
    if not isinstance(action, str) or action not in PERMISSION_COMMANDS:
        raise InvalidArgumentError("unknown recording permission action")


def parse_permission_result(data: bytes, permission: str, action: str) -> dict[str, Any]:
    """Reject malformed/mismatched helper output before it can enter the replay cache."""
    try:
        result = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise RecorderUnavailableError("permission helper returned invalid JSON") from exc
    if not isinstance(result, dict) or set(result) != RESULT_KEYS:
        raise RecorderUnavailableError("permission helper returned an unexpected result")
    report = result.get("permissions")
    valid_report = isinstance(report, dict) and set(report) == PERMISSIONS
    if valid_report:
        valid_report = all(
            isinstance(value, str) and value in PERMISSION_STATUSES for value in report.values()
        )
    if (
        result.get("permission") != permission
        or result.get("action") != action
        or type(result.get("settings_opened")) is not bool
        or result["settings_opened"] != (action == "open_settings")
        or not valid_report
    ):
        raise RecorderUnavailableError("permission helper returned inconsistent permission state")
    return result


class PermissionSetup:
    """Own only the helper PID, never Settings.app or a process group it may open."""

    def __init__(self, *, timeout: float = PERMISSION_ACTION_TIMEOUT) -> None:
        self._timeout = timeout
        self._lock = threading.Lock()
        self._reap_lock = threading.Lock()
        self._proc: subprocess.Popen[bytes] | None = None
        self._executing = False
        self._closed = False

    @property
    def in_progress(self) -> bool:
        with self._lock:
            return self._executing or (self._proc is not None and self._proc.poll() is None)

    def run(self, command: Sequence[str], permission: str, action: str) -> dict[str, Any]:
        validate_action(permission, action)
        with self._lock:
            if self._closed:
                raise RecorderUnavailableError("permission helper is shutting down")
            if self._executing or (self._proc is not None and self._proc.poll() is None):
                raise BusyError("a recording permission action is already running")
            # Spawn and assignment are atomic with respect to abort: shutdown cannot miss a PID.
            try:
                proc = subprocess.Popen(  # noqa: S603 - fixed command and enum arguments, no shell
                    [*command, PERMISSION_COMMANDS[action], permission],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
            except OSError as exc:
                raise RecorderUnavailableError(
                    "cannot launch recording permission helper", details={"errno": exc.errno}
                ) from exc
            self._proc = proc
            self._executing = True
        try:
            try:
                stdout, _stderr = proc.communicate(timeout=self._timeout)
            except subprocess.TimeoutExpired as exc:
                raise RecorderUnavailableError(
                    "recording permission action timed out; check permissions before retrying",
                    details={"timeout_sec": self._timeout},
                ) from exc
            except (OSError, subprocess.SubprocessError) as exc:
                raise RecorderUnavailableError(
                    "cannot read recording permission helper result"
                ) from exc
            with self._lock:
                if self._closed:
                    raise RecorderUnavailableError("recording permission action was aborted")
            if proc.returncode != 0:
                raise RecorderUnavailableError(
                    "recording permission helper failed", details={"returncode": proc.returncode}
                )
            return parse_permission_result(stdout, permission, action)
        finally:
            try:
                self._reap(proc)
            finally:
                with self._lock:
                    self._executing = False
                    # A failed reap stays observable as busy while its PID remains alive.
                    if proc.poll() is not None:
                        self._proc = None
                if proc.poll() is not None:
                    for stream in (proc.stdout, proc.stderr):
                        if stream is not None:
                            stream.close()

    def abort(self) -> None:
        """Prevent future permission spawns and reap the owned child during shutdown."""
        with self._lock:
            self._closed = True
            proc = self._proc
        if proc is not None:
            self._reap(proc)

    def _reap(self, proc: subprocess.Popen[bytes]) -> None:
        # The action thread and shutdown may race; only one signals/waits at a time.
        with self._reap_lock:
            if proc.poll() is not None:
                proc.wait()
                return
            try:
                proc.terminate()
                proc.wait(timeout=PERMISSION_EXIT_GRACE)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                    proc.wait(timeout=PERMISSION_EXIT_GRACE)
                except (OSError, subprocess.TimeoutExpired) as exc:
                    raise RecorderUnavailableError(
                        "recording permission helper could not be reaped"
                    ) from exc
            except OSError as exc:
                if proc.poll() is None:
                    raise RecorderUnavailableError(
                        "recording permission helper could not be stopped"
                    ) from exc
