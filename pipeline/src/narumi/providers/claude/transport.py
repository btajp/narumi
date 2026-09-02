"""Cancellation-safe subprocess transport for the private Claude SDK worker."""

from __future__ import annotations

import json
import os
import selectors
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from narumi.errors import CancelledError, EngineUnavailableError
from narumi.providers.claude.protocol import (
    MAX_RESPONSE_BYTES,
    WorkerRequest,
    WorkerResponse,
    decode_response,
    encode_request,
)

DEFAULT_TIMEOUT = 600.0
MAX_TIMEOUT = 3600.0
POLL_INTERVAL = 0.05
PROCESS_REAP_TIMEOUT = 0.5
TERM_GROUP_TIMEOUT = 1.5
KILL_GROUP_TIMEOUT = 1.5
STATE_INSPECTION_TIMEOUT = 1.0
WATCHDOG_START_TIMEOUT = 3.0
WATCHDOG_EXIT_TIMEOUT = TERM_GROUP_TIMEOUT + KILL_GROUP_TIMEOUT + 2.0
OUTCOME_UNKNOWN = "provider_generation_outcome_unknown"
LIFELINE_ENV = "NARUMI_PARENT_LIFELINE_FD"

# This program is executed from the already-resident, resource-bound transport
# source. It is deliberately stdlib-only and receives no request bytes or
# credentials. The watchdog is the direct parent of the real worker so it can
# keep the worker leader unreaped, reserving the PGID until TERM/KILL cleanup is
# complete even when either the server or worker is killed with SIGKILL.
_WATCHDOG_PROGRAM = r"""
import json
import os
import select
import signal
import stat
import subprocess
import sys
import time

POLL = 0.05
TERM_TIMEOUT = 1.5
KILL_TIMEOUT = 1.5
PS_TIMEOUT = 1.0

def close_fd(descriptor):
    try:
        os.close(descriptor)
    except OSError:
        pass

def read_config(descriptor):
    chunks = []
    size = 0
    while True:
        block = os.read(descriptor, 65536)
        if not block:
            break
        size += len(block)
        if size > 131072:
            raise RuntimeError("watchdog config is too large")
        chunks.append(block)
    value = json.loads(b"".join(chunks))
    if not isinstance(value, dict):
        raise RuntimeError("invalid watchdog config")
    return value

def child_status(process):
    status = os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
    if status is None:
        return None
    if status.si_pid != process.pid:
        raise RuntimeError("unexpected worker identity")
    if status.si_code == os.CLD_EXITED:
        return int(status.si_status)
    if status.si_code in {os.CLD_KILLED, os.CLD_DUMPED}:
        return -int(status.si_status)
    raise RuntimeError("unexpected worker status")

def reserved(process):
    try:
        status = child_status(process)
        if status is not None:
            return True
        try:
            return os.getpgid(process.pid) == process.pid
        except ProcessLookupError:
            # Darwin may hide getpgid() for a just-exited zombie. Re-read the
            # direct-child status: WNOWAIT still pins its PID/PGID identity.
            return child_status(process) is not None
    except (ChildProcessError, OSError, RuntimeError):
        return False

def group_state(group):
    proc = "/proc"
    if os.path.isdir(proc):
        states = []
        try:
            entries = os.listdir(proc)
        except OSError:
            return None
        for entry in entries:
            if not entry.isdigit():
                continue
            try:
                with open(os.path.join(proc, entry, "stat"), "r", encoding="ascii") as source:
                    raw = source.read()
                fields = raw[raw.rindex(") ") + 2:].split()
                if len(fields) >= 3 and int(fields[2]) == group:
                    states.append(fields[0])
            except (OSError, ValueError):
                continue
        return bool(states), any(value not in {"Z", "X"} for value in states)
    try:
        result = subprocess.run(
            ["/bin/ps", "-axo", "pgid=,state="],
            check=True,
            capture_output=True,
            text=True,
            timeout=PS_TIMEOUT,
            env={"PATH": "/usr/bin:/bin", "LANG": "C"},
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    states = []
    for line in result.splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[0].isdigit() and int(fields[0]) == group:
            states.append(fields[1][:1])
    return bool(states), any(value not in {"Z", "X"} for value in states)

def signal_group(process, requested):
    if not reserved(process):
        return False
    try:
        os.killpg(process.pid, requested)
    except ProcessLookupError:
        return True
    except PermissionError:
        state = group_state(process.pid)
        return state is not None and not state[1]
    except OSError:
        return False
    return True

def wait_quiescent(process, duration):
    deadline = time.monotonic() + duration
    while True:
        state = group_state(process.pid)
        if state is not None and not state[1]:
            return True
        if state is None or time.monotonic() >= deadline:
            return False
        time.sleep(POLL)

def cleanup(process):
    if not signal_group(process, signal.SIGTERM):
        return False
    if not wait_quiescent(process, TERM_TIMEOUT):
        if not signal_group(process, signal.SIGKILL):
            return False
        if not wait_quiescent(process, KILL_TIMEOUT):
            return False
    try:
        process.wait(timeout=0.5)
    except (OSError, subprocess.SubprocessError):
        return False
    return process.returncode is not None

def lifeline_closed(descriptor):
    readable, _, _ = select.select([descriptor], [], [], 0)
    if not readable:
        return False
    try:
        return os.read(descriptor, 1) == b""
    except BlockingIOError:
        return False

def main():
    signal.signal(signal.SIGCHLD, signal.SIG_DFL)
    config_fd, lifeline_fd, status_fd = (int(value) for value in sys.argv[1:4])
    config = read_config(config_fd)
    close_fd(config_fd)
    if lifeline_closed(lifeline_fd):
        close_fd(lifeline_fd)
        close_fd(status_fd)
        return 3
    command = config["command"]
    environment = config["environment"]
    cwd = config["cwd"]
    worker_input = int(config["worker_input"])
    worker_output = int(config["worker_output"])
    inherited = tuple(int(value) for value in config["inherited_fds"])
    held = frozenset(int(value) for value in config["watchdog_held_fds"])
    if not held.issubset(inherited):
        raise RuntimeError("invalid watchdog lifetime descriptors")
    process = None
    try:
        process = subprocess.Popen(
            command,
            stdin=worker_input,
            stdout=worker_output,
            stderr=subprocess.DEVNULL,
            cwd=cwd,
            env=environment,
            bufsize=0,
            close_fds=True,
            start_new_session=True,
            umask=0o077,
            pass_fds=inherited,
        )
        if os.getpgid(process.pid) != process.pid:
            raise RuntimeError("worker process group is not isolated")
        close_fd(worker_input)
        close_fd(worker_output)
        for descriptor in inherited:
            if descriptor not in held:
                close_fd(descriptor)
        os.write(status_fd, (str(process.pid) + "\n").encode("ascii"))
        close_fd(status_fd)
        for descriptor in (0, 1, 2):
            close_fd(descriptor)
        status = None
        parent_gone = False
        while status is None and not parent_gone:
            status = child_status(process)
            if status is not None:
                break
            readable, _, _ = select.select([lifeline_fd], [], [], POLL)
            if readable:
                parent_gone = os.read(lifeline_fd, 1) == b""
        close_fd(lifeline_fd)
        if not cleanup(process):
            return 4
        if parent_gone:
            return 2
        return 0 if status == 0 else 1
    except BaseException:
        close_fd(status_fd)
        close_fd(lifeline_fd)
        if process is not None:
            cleanup(process)
        return 4
    finally:
        close_fd(worker_input)
        close_fd(worker_output)
        for descriptor in inherited:
            close_fd(descriptor)

raise SystemExit(main())
"""


@dataclass
class _SupervisedProcess:
    watchdog: subprocess.Popen[bytes]
    stdin: BinaryIO
    stdout: BinaryIO
    lifeline_write: int
    worker_group: int
    worker_identity: tuple[int, str]


class SubprocessWorkerRunner:
    def __init__(
        self,
        command: Sequence[str],
        *,
        inherited_fds: Sequence[int] = (),
        watchdog_held_fds: Sequence[int] = (),
    ) -> None:
        self.command = tuple(command)
        self.inherited_fds = tuple(inherited_fds)
        self.watchdog_held_fds = tuple(watchdog_held_fds)
        if os.name == "posix" and (
            any(type(descriptor) is not int or descriptor < 3 for descriptor in self.inherited_fds)
            or len(set(self.inherited_fds)) != len(self.inherited_fds)
            or any(
                type(descriptor) is not int or descriptor < 3
                for descriptor in self.watchdog_held_fds
            )
            or len(set(self.watchdog_held_fds)) != len(self.watchdog_held_fds)
            or not set(self.watchdog_held_fds).issubset(self.inherited_fds)
        ):
            raise ValueError("invalid inherited worker descriptors")

    def __call__(
        self,
        request: WorkerRequest,
        *,
        env: Mapping[str, str],
        cwd: Path,
        should_cancel: Callable[[], bool],
        timeout: float | None,
    ) -> WorkerResponse:
        duration = DEFAULT_TIMEOUT if timeout is None else timeout
        if type(duration) not in {int, float} or not 0 < duration <= MAX_TIMEOUT:
            raise EngineUnavailableError(
                "Claude SDK worker timeout is invalid",
                details={"reason": "claude_sdk_invalid_timeout", "outcome_unknown": False},
            )
        if should_cancel():
            raise _cancelled(False)
        payload = encode_request(request)
        attempt = [False]
        process: subprocess.Popen[bytes] | _SupervisedProcess | None = None
        try:
            child_env = dict(env)
            if os.name == "posix":
                process = _start_supervised_process(
                    self.command,
                    child_env,
                    cwd,
                    self.inherited_fds,
                    self.watchdog_held_fds,
                )
            else:
                process = subprocess.Popen(
                    self.command,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    cwd=cwd,
                    env=child_env,
                    bufsize=0,
                    close_fds=True,
                    start_new_session=True,
                    umask=0o077,
                )
            if process.stdin is None or process.stdout is None:
                raise OSError("worker pipes unavailable")
            os.set_blocking(process.stdin.fileno(), False)
            os.set_blocking(process.stdout.fileno(), False)
            deadline = time.monotonic() + duration
            response, exit_code = self._exchange(process, payload, deadline, should_cancel, attempt)
            if exit_code != 0:
                raise _unknown() if attempt[0] else _unavailable()
            return decode_response(response)
        except CancelledError:
            raise
        except EngineUnavailableError:
            raise
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
            raise _unknown() if attempt[0] else _unavailable() from None
        finally:
            cleanup_ok = True
            if process is not None:
                cleanup_ok = (
                    _terminate_supervised(process)
                    if isinstance(process, _SupervisedProcess)
                    else _terminate(process)
                )
            if not cleanup_ok:
                raise (
                    _unknown() if attempt[0] else _unavailable("claude_sdk_worker_cleanup_failed")
                )

    def _exchange(
        self,
        process: subprocess.Popen[bytes] | _SupervisedProcess,
        payload: bytes,
        deadline: float,
        should_cancel: Callable[[], bool],
        attempt: list[bool],
    ) -> tuple[bytes, int]:
        assert process.stdin is not None and process.stdout is not None
        sent = 0
        output = bytearray()
        stdin_open = True
        stdout_open = True
        exit_code: int | None = None
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdin, selectors.EVENT_WRITE, "stdin")
            selector.register(process.stdout, selectors.EVENT_READ, "stdout")
            while stdout_open or exit_code is None:
                if should_cancel():
                    raise _cancelled(attempt[0])
                if time.monotonic() >= deadline:
                    raise (_unknown() if attempt[0] else _unavailable("claude_sdk_worker_timeout"))
                for key, _ in selector.select(POLL_INTERVAL):
                    if key.data == "stdin" and stdin_open:
                        try:
                            count = os.write(process.stdin.fileno(), payload[sent : sent + 65536])
                        except BlockingIOError:
                            continue
                        if count <= 0:
                            raise OSError("worker stdin closed")
                        attempt[0] = True
                        sent += count
                        if sent == len(payload):
                            selector.unregister(process.stdin)
                            process.stdin.close()
                            stdin_open = False
                    elif key.data == "stdout" and stdout_open:
                        try:
                            block = os.read(process.stdout.fileno(), 65536)
                        except BlockingIOError:
                            continue
                        if block:
                            output.extend(block)
                            if len(output) > MAX_RESPONSE_BYTES + 1:
                                raise ValueError("worker response is too large")
                        else:
                            selector.unregister(process.stdout)
                            process.stdout.close()
                            stdout_open = False
                if exit_code is None:
                    exit_code = _peek_exit_code(
                        process.watchdog if isinstance(process, _SupervisedProcess) else process
                    )
                    if (
                        isinstance(process, _SupervisedProcess)
                        and exit_code is not None
                        and exit_code != 0
                    ):
                        break
                if not stdout_open and exit_code is not None:
                    break
        assert exit_code is not None
        return bytes(output), exit_code


def _start_supervised_process(
    command: Sequence[str],
    environment: Mapping[str, str],
    cwd: Path,
    inherited_fds: Sequence[int],
    watchdog_held_fds: Sequence[int],
) -> _SupervisedProcess:
    config_read, config_write = os.pipe()
    worker_input, request_write = os.pipe()
    response_read, worker_output = os.pipe()
    lifeline_read, lifeline_write = os.pipe()
    status_read, status_write = os.pipe()
    descriptors = {
        config_read,
        config_write,
        worker_input,
        request_write,
        response_read,
        worker_output,
        lifeline_read,
        lifeline_write,
        status_read,
        status_write,
    }
    watchdog: subprocess.Popen[bytes] | None = None
    try:
        config = json.dumps(
            {
                "command": list(command),
                "environment": dict(environment),
                "cwd": os.fspath(cwd),
                "worker_input": worker_input,
                "worker_output": worker_output,
                "inherited_fds": list(inherited_fds),
                "watchdog_held_fds": list(watchdog_held_fds),
            },
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("ascii")
        if len(config) > 131072:
            raise OSError("watchdog config is too large")
        watchdog_env = {
            "PATH": "/usr/bin:/bin",
            "LANG": "C",
            "LC_ALL": "C",
            "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            LIFELINE_ENV: str(lifeline_read),
        }
        watchdog = subprocess.Popen(
            (
                sys.executable,
                "-I",
                "-S",
                "-c",
                _WATCHDOG_PROGRAM,
                str(config_read),
                str(lifeline_read),
                str(status_write),
            ),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=watchdog_env,
            bufsize=0,
            close_fds=True,
            start_new_session=True,
            umask=0o077,
            pass_fds=(
                config_read,
                worker_input,
                worker_output,
                lifeline_read,
                status_write,
                *inherited_fds,
            ),
        )
        for descriptor in (config_read, worker_input, worker_output, lifeline_read, status_write):
            _close_descriptor(descriptor)
            descriptors.discard(descriptor)
        _write_descriptor(config_write, config)
        _close_descriptor(config_write)
        descriptors.discard(config_write)
        group = _read_watchdog_status(status_read, watchdog)
        identity = _process_identity(group, expected_group=group)
        if identity is None:
            raise OSError("Claude worker identity is unavailable")
        _close_descriptor(status_read)
        descriptors.discard(status_read)
        input_stream = os.fdopen(request_write, "wb", buffering=0)
        descriptors.discard(request_write)
        output_stream = os.fdopen(response_read, "rb", buffering=0)
        descriptors.discard(response_read)
        descriptors.discard(lifeline_write)
        return _SupervisedProcess(
            watchdog,
            input_stream,
            output_stream,
            lifeline_write,
            group,
            identity,
        )
    except BaseException:
        for descriptor in descriptors:
            _close_descriptor(descriptor)
        if watchdog is not None:
            try:
                watchdog.wait(timeout=WATCHDOG_EXIT_TIMEOUT)
            except subprocess.TimeoutExpired:
                try:
                    watchdog.kill()
                    watchdog.wait(timeout=PROCESS_REAP_TIMEOUT)
                except (OSError, subprocess.SubprocessError):
                    pass
        raise


def _read_watchdog_status(descriptor: int, watchdog: subprocess.Popen[bytes]) -> int:
    deadline = time.monotonic() + WATCHDOG_START_TIMEOUT
    output = bytearray()
    with selectors.DefaultSelector() as selector:
        selector.register(descriptor, selectors.EVENT_READ)
        while True:
            if time.monotonic() >= deadline:
                raise OSError("Claude watchdog startup timed out")
            events = selector.select(POLL_INTERVAL)
            if events:
                block = os.read(descriptor, 32)
                if not block:
                    raise OSError("Claude watchdog exited before worker startup")
                output.extend(block)
                if len(output) > 32:
                    raise OSError("invalid Claude watchdog status")
                if b"\n" in output:
                    line, remainder = bytes(output).split(b"\n", 1)
                    if remainder or not line.isdigit():
                        raise OSError("invalid Claude watchdog status")
                    group = int(line)
                    if group <= 1:
                        raise OSError("invalid Claude worker process group")
                    return group
            if _peek_exit_code(watchdog) is not None:
                raise OSError("Claude watchdog failed to start worker")


def _write_descriptor(descriptor: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        count = os.write(descriptor, payload[offset:])
        if count <= 0:
            raise OSError("Claude watchdog config pipe closed")
        offset += count


def _close_descriptor(descriptor: int) -> None:
    try:
        os.close(descriptor)
    except OSError:
        pass


def _terminate_supervised(process: _SupervisedProcess) -> bool:
    for stream in (process.stdin, process.stdout):
        try:
            stream.close()
        except OSError:
            pass
    _close_descriptor(process.lifeline_write)
    try:
        process.watchdog.wait(timeout=WATCHDOG_EXIT_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired):
        try:
            process.watchdog.kill()
            process.watchdog.wait(timeout=PROCESS_REAP_TIMEOUT)
        except (OSError, subprocess.SubprocessError):
            return False
    state = _group_state(process.worker_group)
    if process.watchdog.returncode is None or state is None:
        return False
    if not state[1]:
        return True
    return _terminate_unwatched_group(
        process.worker_group,
        process.worker_identity,
    )


def _terminate_unwatched_group(group: int, leader_identity: tuple[int, str]) -> bool:
    identities = _group_identities(group)
    if identities is None or leader_identity not in identities:
        return False
    if not _signal_identified_group(group, signal.SIGTERM, identities):
        return False
    if _wait_identified_group_quiescent(group, TERM_GROUP_TIMEOUT):
        return True
    if not _signal_identified_group(group, signal.SIGKILL, identities):
        return False
    return _wait_identified_group_quiescent(group, KILL_GROUP_TIMEOUT)


def _signal_identified_group(
    group: int,
    requested_signal: signal.Signals,
    expected: set[tuple[int, str]],
) -> bool:
    current = _group_identities(group)
    if current is None or not current.intersection(expected):
        return False
    try:
        os.killpg(group, requested_signal)
    except ProcessLookupError:
        return True
    except PermissionError:
        state = _group_state(group)
        return state is not None and not state[1]
    except OSError:
        return False
    return True


def _wait_identified_group_quiescent(group: int, duration: float) -> bool:
    deadline = time.monotonic() + duration
    while True:
        state = _group_state(group)
        if state is not None and not state[1]:
            return True
        if state is None or time.monotonic() >= deadline:
            return False
        time.sleep(POLL_INTERVAL)


def _terminate(process: subprocess.Popen[bytes]) -> bool:
    for stream in (process.stdin, process.stdout):
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass
    if os.name != "posix":
        if process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=TERM_GROUP_TIMEOUT)
            except (OSError, subprocess.SubprocessError):
                try:
                    process.kill()
                    process.wait(timeout=KILL_GROUP_TIMEOUT)
                except (OSError, subprocess.SubprocessError):
                    return False
        try:
            process.wait(timeout=0)
        except (OSError, subprocess.SubprocessError):
            return False
        return process.poll() is not None

    group = process.pid
    if not _has_reserved_identity(process):
        return False
    if not _signal_group(group, signal.SIGTERM):
        return False
    if _wait_group_absent(group, TERM_GROUP_TIMEOUT, leader=process):
        return _reaped(process, PROCESS_REAP_TIMEOUT)
    if not _has_reserved_identity(process):
        return False
    if not _signal_group(group, signal.SIGKILL):
        return False
    return _wait_group_quiescent(group, KILL_GROUP_TIMEOUT, leader=process) and _reaped(
        process, PROCESS_REAP_TIMEOUT
    )


def _signal_group(group: int, requested_signal: signal.Signals) -> bool:
    try:
        os.killpg(group, requested_signal)
    except ProcessLookupError:
        return True
    except PermissionError:
        # Darwin reports EPERM for a process group containing only our pinned
        # zombie leader. Accept it only after proving that no live member could
        # have required the signal.
        state = _group_state(group)
        return state is not None and state[1] is False
    except OSError:
        return False
    return True


def _wait_group_absent(
    group: int,
    duration: float,
    *,
    leader: subprocess.Popen[bytes] | None = None,
) -> bool:
    deadline = time.monotonic() + duration
    exit_inspected = False
    while True:
        if leader is not None and not exit_inspected:
            try:
                leader_exited = _peek_exit_code(leader) is not None
            except OSError:
                return False
            if leader_exited:
                # Keep the exited leader unreaped so its PID continues to
                # reserve the PGID until all destructive group signals finish.
                state = _group_state(group)
                exit_inspected = True
                if state is not None and state[1] is False:
                    return True
        try:
            os.killpg(group, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            # Darwin can report EPERM for a just-orphaned/reaped process group
            # even when no live member remains. Inspect once immediately and,
            # if necessary, once more at the deadline; never equate EPERM with
            # successful cleanup without process-state evidence.
            if not exit_inspected:
                state = _group_state(group)
                exit_inspected = True
                if state is not None and state[1] is False:
                    return True
        except OSError:
            return False
        if time.monotonic() >= deadline:
            state = _group_state(group)
            return state is not None and state[1] is False
        time.sleep(POLL_INTERVAL)


def _wait_group_quiescent(
    group: int,
    duration: float,
    *,
    leader: subprocess.Popen[bytes] | None = None,
) -> bool:
    # SIGKILL cannot be ignored. Poll the kernel's process-group existence
    # cheaply first; only invoke the slower platform process-state inspector
    # once at the deadline to distinguish harmless unreaped zombies from a
    # still-live descendant. Repeated /bin/ps spawns were flaky under load.
    if _wait_group_absent(group, duration, leader=leader):
        return True
    state = _group_state(group)
    return state is not None and state[1] is False


def _group_state(group: int) -> tuple[bool, bool] | None:
    proc = Path("/proc")
    if proc.is_dir():
        states: list[str] = []
        try:
            entries = proc.iterdir()
            for entry in entries:
                if not entry.name.isdigit():
                    continue
                try:
                    raw = (entry / "stat").read_text()
                    fields = raw[raw.rindex(") ") + 2 :].split()
                    if len(fields) >= 3 and int(fields[2]) == group:
                        states.append(fields[0])
                except (OSError, ValueError):
                    continue
        except OSError:
            return None
        return bool(states), any(state not in {"Z", "X"} for state in states)
    try:
        output = subprocess.run(
            ["/bin/ps", "-axo", "pgid=,state="],
            check=True,
            capture_output=True,
            text=True,
            timeout=STATE_INSPECTION_TIMEOUT,
            env={"PATH": "/usr/bin:/bin", "LANG": "C"},
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    states = []
    for line in output.splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[0].isdigit() and int(fields[0]) == group:
            states.append(fields[1][:1])
    return bool(states), any(state not in {"Z", "X"} for state in states)


def _process_identity(pid: int, *, expected_group: int) -> tuple[int, str] | None:
    identities = _group_identities(expected_group)
    if identities is None:
        return None
    return next((identity for identity in identities if identity[0] == pid), None)


def _group_identities(group: int) -> set[tuple[int, str]] | None:
    proc = Path("/proc")
    if proc.is_dir():
        identities: set[tuple[int, str]] = set()
        try:
            entries = proc.iterdir()
            for entry in entries:
                if not entry.name.isdigit():
                    continue
                try:
                    raw = (entry / "stat").read_text()
                    fields = raw[raw.rindex(") ") + 2 :].split()
                    if (
                        len(fields) >= 20
                        and int(fields[2]) == group
                        and fields[0] not in {"Z", "X"}
                    ):
                        identities.add((int(entry.name), fields[19]))
                except (OSError, ValueError):
                    continue
        except OSError:
            return None
        return identities
    try:
        output = subprocess.run(
            ["/bin/ps", "-axo", "pid=,pgid=,state=,lstart="],
            check=True,
            capture_output=True,
            text=True,
            timeout=STATE_INSPECTION_TIMEOUT,
            env={"PATH": "/usr/bin:/bin", "LANG": "C"},
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    identities = set()
    for line in output.splitlines():
        fields = line.split()
        if (
            len(fields) >= 8
            and fields[0].isdigit()
            and fields[1].isdigit()
            and int(fields[1]) == group
            and fields[2][:1] not in {"Z", "X"}
        ):
            identities.add((int(fields[0]), " ".join(fields[3:])))
    return identities


def _peek_exit_code(process: subprocess.Popen[bytes]) -> int | None:
    if os.name != "posix":
        return process.poll()
    try:
        status = os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
    except ChildProcessError:
        raise OSError("worker was reaped before process-group cleanup") from None
    if status is None:
        return None
    if status.si_pid != process.pid:
        raise OSError("unexpected worker wait status")
    if status.si_code == os.CLD_EXITED:
        return int(status.si_status)
    if status.si_code in {os.CLD_KILLED, os.CLD_DUMPED}:
        return -int(status.si_status)
    raise OSError("unexpected worker exit status")


def _has_reserved_identity(process: subprocess.Popen[bytes]) -> bool:
    try:
        _peek_exit_code(process)
    except OSError:
        return False
    return True


def _reaped(process: subprocess.Popen[bytes], duration: float = 0) -> bool:
    try:
        process.wait(timeout=duration)
    except subprocess.TimeoutExpired:
        return False
    except OSError:
        return False
    return process.poll() is not None


def _cancelled(unknown: bool) -> CancelledError:
    return CancelledError(
        "Claude Agent SDK generation was cancelled",
        details={"outcome_unknown": unknown},
    )


def _unavailable(reason: str = "claude_sdk_worker_unavailable") -> EngineUnavailableError:
    return EngineUnavailableError(
        "Claude Agent SDK worker is unavailable",
        details={"reason": reason, "outcome_unknown": False},
    )


def _unknown() -> EngineUnavailableError:
    return EngineUnavailableError(
        "The provider generation outcome is unknown; explicitly start a new attempt to resend",
        details={"reason": OUTCOME_UNKNOWN, "outcome_unknown": True},
    )
