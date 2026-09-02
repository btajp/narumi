"""POSIX parent-death supervision for one private Codex App Server."""

from __future__ import annotations

import errno
import json
import os
import secrets
import selectors
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO

POLL_INTERVAL = 0.05
PROCESS_REAP_TIMEOUT = 0.5
TERM_GROUP_TIMEOUT = 1.5
KILL_GROUP_TIMEOUT = 1.5
STATE_INSPECTION_TIMEOUT = 1.0
WATCHDOG_START_TIMEOUT = 3.0
WATCHDOG_EXIT_TIMEOUT = TERM_GROUP_TIMEOUT + KILL_GROUP_TIMEOUT + 2.0
_ALLOWED_ENVIRONMENT = frozenset(
    {
        "HOME",
        "CODEX_HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
        "TMPDIR",
        "PATH",
        "LANG",
        "LC_ALL",
        "RUST_LOG",
        "NO_COLOR",
    }
)
_fork_lock = threading.Lock()
_child_close_descriptors: dict[int, object] = {}


def _before_fork() -> None:
    _fork_lock.acquire()


def _after_fork_parent() -> None:
    _fork_lock.release()


def _after_fork_child() -> None:
    try:
        for descriptor in tuple(_child_close_descriptors):
            _close(descriptor)
        _child_close_descriptors.clear()
    finally:
        _fork_lock.release()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(
        before=_before_fork,
        after_in_parent=_after_fork_parent,
        after_in_child=_after_fork_child,
    )

# The watchdog receives only fixed launch metadata and pipe numbers. Request bytes,
# API keys, auth files and credential descriptors remain outside this process.
_WATCHDOG_PROGRAM = r"""
import json
import os
import select
import signal
import subprocess
import sys
import time

POLL = 0.05
TERM_TIMEOUT = 1.5
KILL_TIMEOUT = 1.5
PS_TIMEOUT = 1.0
START_TIMEOUT = 3.0
LAUNCHER = r'''\
import os
import sys

request_path, response_path, guardian_path, ready_fd = (
    sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])
)
command = sys.argv[5:]
request_fd = os.open(request_path, os.O_RDONLY)
response_fd = os.open(response_path, os.O_WRONLY)
guardian_fd = os.open(guardian_path, os.O_RDONLY)
os.dup2(request_fd, 0)
os.dup2(response_fd, 1)
if request_fd not in {0, 1}:
    os.close(request_fd)
if response_fd not in {0, 1}:
    os.close(response_fd)
anchor_pid = os.fork()
if anchor_pid == 0:
    os.close(ready_fd)
    for descriptor in (0, 1, 2):
        try:
            os.close(descriptor)
        except OSError:
            pass
    try:
        while os.read(guardian_fd, 1):
            pass
    except OSError:
        pass
    os._exit(0)
os.close(guardian_fd)
os.write(ready_fd, (str(anchor_pid) + "\n").encode("ascii"))
os.close(ready_fd)
os.execvpe(command[0], command, os.environ)
'''

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
        raise RuntimeError("unexpected child identity")
    if status.si_code == os.CLD_EXITED:
        return int(status.si_status)
    if status.si_code in {os.CLD_KILLED, os.CLD_DUMPED}:
        return -int(status.si_status)
    raise RuntimeError("unexpected child status")

def reserved(process):
    try:
        status = child_status(process)
        if status is not None:
            return True
        try:
            return os.getpgid(process.pid) == process.pid
        except ProcessLookupError:
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

def main():
    signal.signal(signal.SIGCHLD, signal.SIG_DFL)
    config_fd, lifeline_fd, status_fd = (int(value) for value in sys.argv[1:4])
    config = read_config(config_fd)
    close_fd(config_fd)
    process = None
    try:
        readable, _, _ = select.select([lifeline_fd], [], [], 0)
        if readable and os.read(lifeline_fd, 1) == b"":
            return 3
        ready_read, ready_write = os.pipe()
        process = subprocess.Popen(
            [
                sys.executable,
                "-I",
                "-S",
                "-c",
                LAUNCHER,
                config["request_path"],
                config["response_path"],
                config["guardian_path"],
                str(ready_write),
                *config["command"],
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=config["cwd"],
            env=config["environment"],
            bufsize=0,
            close_fds=True,
            start_new_session=True,
            umask=0o077,
            pass_fds=(ready_write,),
        )
        if os.getpgid(process.pid) != process.pid:
            raise RuntimeError("child process group is not isolated")
        close_fd(ready_write)
        os.set_blocking(ready_read, False)
        ready = bytearray()
        startup_deadline = time.monotonic() + START_TIMEOUT
        parent_gone = False
        while b"\n" not in ready:
            if child_status(process) is not None:
                raise RuntimeError("child launcher failed")
            if time.monotonic() >= startup_deadline:
                raise RuntimeError("child launcher timed out")
            readable, _, _ = select.select(
                [ready_read, lifeline_fd], [], [], min(POLL, startup_deadline - time.monotonic())
            )
            if lifeline_fd in readable and os.read(lifeline_fd, 1) == b"":
                parent_gone = True
                break
            if ready_read in readable:
                block = os.read(ready_read, 32)
                if not block:
                    raise RuntimeError("child launcher failed")
                ready.extend(block)
                if len(ready) > 32:
                    raise RuntimeError("invalid child launcher status")
        if parent_gone:
            close_fd(ready_read)
            close_fd(status_fd)
            close_fd(lifeline_fd)
            return 2 if cleanup(process) else 4
        if not ready.endswith(b"\n") or not ready[:-1].isdigit():
            raise RuntimeError("child launcher failed")
        anchor_pid = int(ready[:-1])
        if anchor_pid <= 1:
            raise RuntimeError("invalid child anchor")
        close_fd(ready_read)
        os.write(status_fd, f"{process.pid} {anchor_pid}\n".encode("ascii"))
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
        if process is not None:
            cleanup(process)
        return 4
    finally:
        close_fd(status_fd)
        close_fd(lifeline_fd)

raise SystemExit(main())
"""


@dataclass(frozen=True)
class FDLease:
    descriptor: int
    token: object = field(default_factory=object, repr=False)


@dataclass
class SupervisedProcess:
    watchdog: subprocess.Popen[bytes]
    stdin: BinaryIO
    stdout: BinaryIO
    stdin_lease: FDLease
    stdout_lease: FDLease
    lifeline_lease: FDLease
    guardian_lease: FDLease
    pid: int
    identity: tuple[int, str]
    anchor_identity: tuple[int, str]

    def poll(self) -> int | None:
        return self.watchdog.poll()

    def wait(self, timeout: float | None = None) -> int:
        return self.watchdog.wait(timeout=timeout)

    def kill(self) -> None:
        self.watchdog.kill()

    @property
    def lifeline_write(self) -> int:
        return self.lifeline_lease.descriptor

    @property
    def guardian_write(self) -> int:
        return self.guardian_lease.descriptor


def _register_descriptor(descriptor: int) -> FDLease:
    lease = FDLease(descriptor)
    _child_close_descriptors[descriptor] = lease.token
    return lease


def _registered_pipe(owner: set[FDLease] | None = None) -> tuple[FDLease, FDLease]:
    with _fork_lock:
        read_descriptor, write_descriptor = os.pipe()
        read_lease: FDLease | None = None
        write_lease: FDLease | None = None
        try:
            read_lease = _register_descriptor(read_descriptor)
            write_lease = _register_descriptor(write_descriptor)
            if owner is not None:
                owner.update((read_lease, write_lease))
            return read_lease, write_lease
        except BaseException:
            for descriptor, lease in (
                (read_descriptor, read_lease),
                (write_descriptor, write_lease),
            ):
                if lease is not None and _child_close_descriptors.get(descriptor) is lease.token:
                    del _child_close_descriptors[descriptor]
                _close(descriptor)
            if owner is not None:
                if read_lease is not None:
                    owner.discard(read_lease)
                if write_lease is not None:
                    owner.discard(write_lease)
            raise


def _release_descriptor(lease: FDLease) -> bool:
    with _fork_lock:
        if _child_close_descriptors.get(lease.descriptor) is not lease.token:
            return False
        del _child_close_descriptors[lease.descriptor]
        _close(lease.descriptor)
        return True


def _release_stream(stream: BinaryIO, lease: FDLease) -> bool:
    with _fork_lock:
        if _child_close_descriptors.get(lease.descriptor) is not lease.token:
            return False
        stream_closed = True
        try:
            stream.close()
        except OSError:
            stream_closed = False
        del _child_close_descriptors[lease.descriptor]
        _close(lease.descriptor)
        return stream_closed


def _lease_register(selector: selectors.BaseSelector, lease: FDLease, event: int) -> None:
    with _fork_lock:
        if _child_close_descriptors.get(lease.descriptor) is not lease.token:
            raise OSError(errno.EBADF, "descriptor lease is no longer owned")
        selector.register(lease.descriptor, event)


def _lease_read(lease: FDLease, size: int) -> bytes:
    with _fork_lock:
        if _child_close_descriptors.get(lease.descriptor) is not lease.token:
            raise OSError(errno.EBADF, "descriptor lease is no longer owned")
        return os.read(lease.descriptor, size)


def _lease_write(lease: FDLease, payload: bytes) -> int:
    with _fork_lock:
        if _child_close_descriptors.get(lease.descriptor) is not lease.token:
            raise OSError(errno.EBADF, "descriptor lease is no longer owned")
        return os.write(lease.descriptor, payload)


def _open_registered_fifo_reader(path: Path, owner: set[FDLease] | None = None) -> FDLease:
    with _fork_lock:
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
        lease: FDLease | None = None
        try:
            lease = _register_descriptor(descriptor)
            if owner is not None:
                owner.add(lease)
            return lease
        except BaseException:
            if lease is not None and _child_close_descriptors.get(descriptor) is lease.token:
                del _child_close_descriptors[descriptor]
            _close(descriptor)
            if owner is not None and lease is not None:
                owner.discard(lease)
            raise


def _open_registered_fifo_writer(
    path: Path,
    watchdog: subprocess.Popen[bytes],
    owner: set[FDLease] | None = None,
) -> FDLease:
    deadline = time.monotonic() + WATCHDOG_START_TIMEOUT
    while True:
        try:
            with _fork_lock:
                descriptor = os.open(path, os.O_WRONLY | os.O_NONBLOCK | os.O_CLOEXEC)
                lease: FDLease | None = None
                try:
                    lease = _register_descriptor(descriptor)
                    if owner is not None:
                        owner.add(lease)
                    return lease
                except BaseException:
                    if (
                        lease is not None
                        and _child_close_descriptors.get(descriptor) is lease.token
                    ):
                        del _child_close_descriptors[descriptor]
                    _close(descriptor)
                    if owner is not None and lease is not None:
                        owner.discard(lease)
                    raise
        except OSError as error:
            if error.errno != errno.ENXIO or time.monotonic() >= deadline:
                raise
            if watchdog.poll() is not None:
                raise OSError("Codex watchdog failed before guardian connection") from error
            time.sleep(POLL_INTERVAL)


def _create_fifo(path: Path, owner: list[Path]) -> None:
    created = False
    try:
        os.mkfifo(path, 0o600)
        created = True
        owner.append(path)
        created = False
    finally:
        if created:
            try:
                path.unlink()
            except OSError:
                pass


def _cleanup_paths(paths: list[Path]) -> None:
    for path in paths:
        try:
            path.unlink()
        except BaseException:
            # FIFO paths are post-launch housekeeping. Once the process leases
            # have an owner, an asynchronous cleanup exception must not discard
            # the only handle capable of reaping that process group.
            pass


def _cleanup_leases(leases: set[FDLease]) -> None:
    for _ in range(2):
        for lease in tuple(leases):
            try:
                released = _release_descriptor(lease)
            except BaseException:
                continue
            if released or _child_close_descriptors.get(lease.descriptor) is not lease.token:
                leases.discard(lease)


def start(command: list[str], environment: dict[str, str], cwd: Path) -> SupervisedProcess:
    if not set(environment).issubset(_ALLOWED_ENVIRONMENT):
        raise ValueError("Codex child environment is not allowlisted")
    token = secrets.token_hex(16)
    request_path = cwd / f".codex-request-{token}.fifo"
    response_path = cwd / f".codex-response-{token}.fifo"
    guardian_path = cwd / f".codex-guardian-{token}.fifo"
    created_paths: list[Path] = []
    descriptors: set[FDLease] = set()
    try:
        _create_fifo(request_path, created_paths)
        _create_fifo(response_path, created_paths)
        _create_fifo(guardian_path, created_paths)
        config_read, config_write = _registered_pipe(descriptors)
        lifeline_read, lifeline_write = _registered_pipe(descriptors)
        status_read, status_write = _registered_pipe(descriptors)
    except BaseException:
        _cleanup_leases(descriptors)
        _cleanup_paths(created_paths)
        raise
    request_write: FDLease | None = None
    response_read: FDLease | None = None
    guardian_write: FDLease | None = None
    input_stream: BinaryIO | None = None
    output_stream: BinaryIO | None = None
    stdin_lease: FDLease | None = None
    stdout_lease: FDLease | None = None
    watchdog: subprocess.Popen[bytes] | None = None
    managed_process: SupervisedProcess | None = None
    group: int | None = None
    ownership: tuple[tuple[int, str], ...] = ()
    try:
        config = json.dumps(
            {
                "command": command,
                "environment": environment,
                "cwd": os.fspath(cwd),
                "request_path": os.fspath(request_path),
                "response_path": os.fspath(response_path),
                "guardian_path": os.fspath(guardian_path),
            },
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("ascii")
        if len(config) > 131072:
            raise OSError("watchdog config is too large")
        watchdog = subprocess.Popen(
            (
                sys.executable,
                "-I",
                "-S",
                "-c",
                _WATCHDOG_PROGRAM,
                str(config_read.descriptor),
                str(lifeline_read.descriptor),
                str(status_write.descriptor),
            ),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env={
                "PATH": "/usr/bin:/bin",
                "LANG": "C",
                "LC_ALL": "C",
                "PYTHONNOUSERSITE": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
            },
            bufsize=0,
            close_fds=True,
            start_new_session=True,
            umask=0o077,
            pass_fds=(
                config_read.descriptor,
                lifeline_read.descriptor,
                status_write.descriptor,
            ),
        )
        for descriptor in (config_read, lifeline_read, status_write):
            _release_descriptor(descriptor)
            descriptors.discard(descriptor)
        _write(config_write.descriptor, config)
        _release_descriptor(config_write)
        descriptors.discard(config_write)
        response_read = _open_registered_fifo_reader(response_path, descriptors)
        request_deadline = time.monotonic() + WATCHDOG_START_TIMEOUT
        while True:
            try:
                with _fork_lock:
                    request_descriptor = os.open(
                        request_path, os.O_WRONLY | os.O_NONBLOCK | os.O_CLOEXEC
                    )
                    request_write = _register_descriptor(request_descriptor)
                    try:
                        descriptors.add(request_write)
                    except BaseException:
                        if _child_close_descriptors.get(request_descriptor) is request_write.token:
                            del _child_close_descriptors[request_descriptor]
                        _close(request_descriptor)
                        request_write = None
                        raise
                break
            except OSError as error:
                if error.errno != errno.ENXIO or time.monotonic() >= request_deadline:
                    raise
                if watchdog.poll() is not None:
                    raise OSError("Codex watchdog failed before pipe connection") from error
                time.sleep(POLL_INTERVAL)
        guardian_write = _open_registered_fifo_writer(guardian_path, watchdog, descriptors)
        group, anchor_pid = _read_status(status_read.descriptor, watchdog)
        identity = _process_identity(group, expected_group=group)
        anchor_identity = _process_identity(anchor_pid, expected_group=group)
        if identity is None or anchor_identity is None:
            raise OSError("Codex child identity unavailable")
        ownership = (identity, anchor_identity)
        _release_descriptor(status_read)
        descriptors.discard(status_read)
        stdin_lease = request_write
        input_stream = os.fdopen(stdin_lease.descriptor, "wb", buffering=0, closefd=False)
        descriptors.discard(request_write)
        request_write = None
        stdout_lease = response_read
        output_stream = os.fdopen(stdout_lease.descriptor, "rb", buffering=0, closefd=False)
        descriptors.discard(response_read)
        response_read = None
        assert guardian_write is not None
        managed_process = SupervisedProcess(
            watchdog,
            input_stream,
            output_stream,
            stdin_lease,
            stdout_lease,
            lifeline_write,
            guardian_write,
            group,
            identity,
            anchor_identity,
        )
        return managed_process
    except BaseException:
        if managed_process is not None:
            for _ in range(2):
                try:
                    terminate(managed_process)
                    break
                except BaseException:
                    continue
        else:
            _cleanup_leases(descriptors)
        if watchdog is not None:
            try:
                watchdog.wait(timeout=WATCHDOG_EXIT_TIMEOUT)
            except BaseException:
                try:
                    watchdog.kill()
                    watchdog.wait(timeout=PROCESS_REAP_TIMEOUT)
                except BaseException:
                    pass
        if group is not None and ownership:
            try:
                _terminate_unwatched(group, ownership)
            except BaseException:
                pass
        raise
    finally:
        if managed_process is None:
            if input_stream is not None:
                assert stdin_lease is not None
                try:
                    _release_stream(input_stream, stdin_lease)
                except BaseException:
                    pass
            if output_stream is not None:
                assert stdout_lease is not None
                try:
                    _release_stream(output_stream, stdout_lease)
                except BaseException:
                    pass
            if request_write is not None:
                try:
                    _release_descriptor(request_write)
                except BaseException:
                    pass
            if response_read is not None:
                try:
                    _release_descriptor(response_read)
                except BaseException:
                    pass
            if guardian_write is not None:
                try:
                    _release_descriptor(guardian_write)
                except BaseException:
                    pass
            _cleanup_leases(descriptors)
        _cleanup_paths(created_paths)


def terminate(process: SupervisedProcess) -> bool:
    pending: BaseException | None = None

    def cleanup(action) -> None:
        nonlocal pending
        try:
            action()
        except BaseException as error:
            if pending is None:
                pending = error

    cleanup(lambda: _release_stream(process.stdin, process.stdin_lease))
    cleanup(lambda: _release_stream(process.stdout, process.stdout_lease))
    cleanup(lambda: _release_descriptor(process.lifeline_lease))
    result = False
    try:
        try:
            process.watchdog.wait(timeout=WATCHDOG_EXIT_TIMEOUT)
        except subprocess.TimeoutExpired:
            pass
        except BaseException as error:
            if pending is None:
                pending = error
        if process.watchdog.poll() is None:
            try:
                process.watchdog.kill()
                process.watchdog.wait(timeout=PROCESS_REAP_TIMEOUT)
            except BaseException as error:
                if pending is None:
                    pending = error
        try:
            result = _finish_cleanup(process)
        except BaseException as error:
            if pending is None:
                pending = error
    finally:
        cleanup(lambda: _release_stream(process.stdin, process.stdin_lease))
        cleanup(lambda: _release_stream(process.stdout, process.stdout_lease))
        cleanup(lambda: _release_descriptor(process.lifeline_lease))
        cleanup(lambda: _release_descriptor(process.guardian_lease))
    if pending is not None:
        raise pending
    return result


def _finish_cleanup(process: SupervisedProcess) -> bool:
    state = _group_state(process.pid)
    if process.watchdog.returncode is None or state is None:
        return False
    if not state[1]:
        return True
    return _terminate_unwatched(process.pid, (process.identity, process.anchor_identity))


def _terminate_unwatched(group: int, ownership: tuple[tuple[int, str], ...]) -> bool:
    identities = _group_identities(group)
    if identities is None or not identities.intersection(ownership):
        return False
    if not _signal_identified(group, signal.SIGTERM, identities):
        return False
    if _wait_quiescent(group, TERM_GROUP_TIMEOUT):
        return True
    if not _signal_identified(group, signal.SIGKILL, identities):
        return False
    return _wait_quiescent(group, KILL_GROUP_TIMEOUT)


def _signal_identified(
    group: int, requested: signal.Signals, expected: set[tuple[int, str]]
) -> bool:
    current = _group_identities(group)
    if current is None or not current.intersection(expected):
        return False
    try:
        os.killpg(group, requested)
    except ProcessLookupError:
        return True
    except PermissionError:
        state = _group_state(group)
        return state is not None and not state[1]
    except OSError:
        return False
    return True


def _wait_quiescent(group: int, duration: float) -> bool:
    deadline = time.monotonic() + duration
    while True:
        state = _group_state(group)
        if state is not None and not state[1]:
            return True
        if state is None or time.monotonic() >= deadline:
            return False
        time.sleep(POLL_INTERVAL)


def _read_status(descriptor: int, watchdog: subprocess.Popen[bytes]) -> tuple[int, int]:
    deadline = time.monotonic() + WATCHDOG_START_TIMEOUT
    output = bytearray()
    with selectors.DefaultSelector() as selector:
        selector.register(descriptor, selectors.EVENT_READ)
        while True:
            if time.monotonic() >= deadline:
                raise OSError("Codex watchdog startup timed out")
            if selector.select(POLL_INTERVAL):
                block = os.read(descriptor, 32)
                if not block:
                    raise OSError("Codex watchdog exited before child startup")
                output.extend(block)
                if len(output) > 32:
                    raise OSError("invalid Codex watchdog status")
                if b"\n" in output:
                    line, remainder = bytes(output).split(b"\n", 1)
                    fields = line.split()
                    if (
                        remainder
                        or len(fields) != 2
                        or not all(field.isdigit() for field in fields)
                    ):
                        raise OSError("invalid Codex watchdog status")
                    group, anchor = (int(field) for field in fields)
                    if group <= 1 or anchor <= 1 or group == anchor:
                        raise OSError("invalid Codex process group")
                    return group, anchor
            if watchdog.poll() is not None:
                raise OSError("Codex watchdog failed to start child")


def _write(descriptor: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        count = os.write(descriptor, payload[offset:])
        if count <= 0:
            raise OSError("Codex watchdog config pipe closed")
        offset += count


def _close(descriptor: int) -> None:
    try:
        os.close(descriptor)
    except OSError:
        pass


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
