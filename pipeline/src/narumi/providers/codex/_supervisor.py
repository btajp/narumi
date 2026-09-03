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

from narumi.providers.codex import _process_tree
from narumi.providers.codex._process_tree import WATCHDOG_PROGRAM

POLL_INTERVAL = 0.05
PROCESS_REAP_TIMEOUT = 0.5
TERM_GROUP_TIMEOUT = 1.5
KILL_GROUP_TIMEOUT = 1.5
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
    ownership_marker: str
    descendant_identities: set[tuple[int, str]] = field(default_factory=set)

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
    child_environment = dict(environment)
    child_environment[_process_tree.OWNERSHIP_ENV] = token
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
                "environment": child_environment,
                "cwd": os.fspath(cwd),
                "request_path": os.fspath(request_path),
                "response_path": os.fspath(response_path),
                "guardian_path": os.fspath(guardian_path),
                "ownership_marker": token,
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
                WATCHDOG_PROGRAM,
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
            token,
            {identity, anchor_identity},
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

    descendants, _ = _freeze_identities(
        process.pid,
        process.descendant_identities,
        marker=process.ownership_marker,
        required_identity=process.identity,
    )
    process.descendant_identities = descendants

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
    status = process.watchdog.returncode
    if status is None or state is None:
        return False
    active = _active_identities(process.descendant_identities)
    if active is None:
        return False
    cleaned = not state[1] and not active
    if not cleaned:
        cleaned = _terminate_unwatched(
            process.pid,
            tuple(process.descendant_identities),
            marker=process.ownership_marker,
        )
    return cleaned and status in {0, 1, 2}


def _terminate_unwatched(
    group: int,
    ownership: tuple[tuple[int, str], ...],
    *,
    marker: str | None = None,
) -> bool:
    owned, frozen = _freeze_identities(group, set(ownership), marker=marker)
    killed = _kill_identities(group, owned, marker=marker)
    return frozen and killed


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


def _freeze_identities(
    group: int,
    ownership: set[tuple[int, str]],
    *,
    marker: str | None = None,
    required_identity: tuple[int, str] | None = None,
) -> tuple[set[tuple[int, str]], bool]:
    deadline = time.monotonic() + TERM_GROUP_TIMEOUT
    owned = set(ownership)
    first = True
    while True:
        processes = _process_table()
        if processes is None:
            return owned, False
        if marker is not None:
            marked = _process_tree.marked_identities(marker, processes)
            if marked is None:
                return owned, False
            owned.update(marked)
        active = {
            identity
            for identity in owned
            if (process := processes.get(identity[0])) is not None
            and process[3] == identity[1]
            and process[2] not in {"Z", "X"}
        }
        if first and required_identity is not None and required_identity not in active:
            return owned, False
        first = False
        owned = _extend_descendant_identities(owned, processes)
        group_identities = {
            (pid, started)
            for pid, (_, process_group, state, started) in processes.items()
            if process_group == group and state not in {"Z", "X"}
        }
        group_owned = group_identities & owned
        if group_owned:
            owned.update(group_identities)
            owned = _extend_descendant_identities(owned, processes)
            if not _signal_identified(group, signal.SIGSTOP, group_owned):
                return owned, False
        active = _active_identities(owned)
        if active is None or not active:
            return owned, False
        if not _signal_identities(active, signal.SIGSTOP, missing_ok=marker is not None):
            return owned, False

        verified = _process_table()
        if verified is None:
            return owned, False
        expanded = _extend_descendant_identities(owned, verified)
        if marker is not None:
            marked = _process_tree.marked_identities(marker, verified)
            if marked is None:
                return owned, False
            expanded.update(marked)
        verified_group = {
            (pid, started)
            for pid, (_, process_group, state, started) in verified.items()
            if process_group == group and state not in {"Z", "X"}
        }
        if verified_group & expanded:
            expanded.update(verified_group)
        if expanded != owned:
            owned = expanded
            continue
        states = [
            process[2]
            for pid, started in owned
            if (process := verified.get(pid)) is not None
            and process[3] == started
            and process[2] not in {"Z", "X"}
        ]
        if states and all(state == "T" for state in states):
            return owned, True
        if time.monotonic() >= deadline:
            return owned, False
        time.sleep(POLL_INTERVAL)


def _kill_identities(
    group: int,
    ownership: set[tuple[int, str]],
    *,
    marker: str | None = None,
) -> bool:
    identities = _group_identities(group)
    active = _active_identities(ownership)
    if identities is None or active is None:
        return False
    group_owned = identities & ownership
    success = True
    if group_owned:
        success = _signal_identified(group, signal.SIGKILL, group_owned)
    success = _signal_identities(active, signal.SIGKILL) and success
    return success and _wait_cleanup(group, ownership, KILL_GROUP_TIMEOUT, marker=marker)


def _wait_cleanup(
    group: int,
    ownership: set[tuple[int, str]],
    duration: float,
    *,
    marker: str | None = None,
) -> bool:
    deadline = time.monotonic() + duration
    while True:
        state = _group_state(group)
        active = _active_identities(ownership)
        marked = _process_tree.marked_identities(marker) if marker is not None else set()
        if (
            state is not None
            and active is not None
            and marked is not None
            and not state[1]
            and not active
            and not marked
        ):
            return True
        if state is None or active is None or marked is None or time.monotonic() >= deadline:
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


_write = _process_tree.write_all
_close = _process_tree.close_fd


_process_table = _process_tree.process_table
_group_state = _process_tree.group_state
_process_identity = _process_tree.process_identity
_group_identities = _process_tree.group_identities
_extend_descendant_identities = _process_tree.extend_descendants
_active_identities = _process_tree.active_identities


def _signal_identities(
    ownership: set[tuple[int, str]],
    requested: signal.Signals,
    *,
    missing_ok: bool = True,
) -> bool:
    active = _active_identities(ownership)
    if active is None:
        return False
    if not missing_ok and active != ownership:
        return False
    for pid, _ in active:
        try:
            os.kill(pid, requested)
        except ProcessLookupError:
            if not missing_ok:
                return False
        except OSError:
            return False
    return True
