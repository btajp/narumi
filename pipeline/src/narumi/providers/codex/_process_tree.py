"""Process identity helpers and the isolated Codex watchdog program."""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
from pathlib import Path

OWNERSHIP_ENV = "NARUMI_CODEX_SUPERVISOR_ID"
STATE_INSPECTION_TIMEOUT = 1.0
ProcessIdentity = tuple[int, str]
ProcessTable = dict[int, tuple[int, int, str, str]]


def close_fd(descriptor: int) -> None:
    try:
        os.close(descriptor)
    except OSError:
        pass


def write_all(descriptor: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        count = os.write(descriptor, payload[offset:])
        if count <= 0:
            raise OSError("Codex watchdog config pipe closed")
        offset += count


WATCHDOG_PROGRAM = r"""
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
OWNER_ENV = "NARUMI_CODEX_SUPERVISOR_ID"
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
    if status.si_code in {os.CLD_STOPPED, os.CLD_CONTINUED}:
        return None
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

def process_table():
    proc = "/proc"
    if os.path.isdir(proc):
        processes = {}
        try:
            entries = os.listdir(proc)
        except OSError:
            return None
        for entry in entries:
            if not entry.isdigit():
                continue
            try:
                with open(os.path.join(proc, entry, "stat"), "rb") as source:
                    raw = source.read()
                fields = raw[raw.rindex(b") ") + 2:].split()
                if len(fields) >= 20:
                    processes[int(entry)] = (
                        int(fields[1]), int(fields[2]),
                        fields[0][:1].decode("ascii"), fields[19].decode("ascii")
                    )
            except (OSError, UnicodeError, ValueError):
                continue
        return processes
    try:
        result = subprocess.run(
            ["/bin/ps", "-axo", "pid=,ppid=,pgid=,state=,lstart="],
            check=True,
            capture_output=True,
            text=True,
            timeout=PS_TIMEOUT,
            env={"PATH": "/usr/bin:/bin", "LANG": "C"},
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    processes = {}
    for line in result.splitlines():
        fields = line.split()
        if (
            len(fields) >= 9
            and fields[0].isdigit()
            and fields[1].isdigit()
            and fields[2].isdigit()
        ):
            processes[int(fields[0])] = (
                int(fields[1]), int(fields[2]), fields[3][:1], " ".join(fields[4:])
            )
    return processes

def marked_identities(marker, processes):
    needle = (OWNER_ENV + "=" + marker).encode("ascii")
    proc = "/proc"
    if os.path.isdir(proc):
        result = set()
        for pid, value in processes.items():
            if value[2] in {"Z", "X"}:
                continue
            path = os.path.join(proc, str(pid))
            try:
                if os.stat(path).st_uid != os.getuid():
                    continue
                with open(os.path.join(path, "environ"), "rb") as source:
                    environment = source.read().split(b"\0")
            except (FileNotFoundError, ProcessLookupError):
                continue
            except OSError:
                return None
            if needle in environment:
                result.add((pid, value[3]))
        return result
    try:
        output = subprocess.run(
            ["/bin/ps", "eww", "-axo", "pid=,command="],
            check=True,
            capture_output=True,
            timeout=PS_TIMEOUT,
            env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    padded = b" " + needle + b" "
    result = set()
    for line in output.splitlines():
        fields = line.split(maxsplit=1)
        if not fields or not fields[0].isdigit():
            continue
        pid = int(fields[0])
        value = processes.get(pid)
        haystack = b" " + (fields[1] if len(fields) == 2 else b"") + b" "
        if value is not None and value[2] not in {"Z", "X"} and padded in haystack:
            result.add((pid, value[3]))
    return result

def group_state(group):
    processes = process_table()
    if processes is None:
        return None
    states = [value[2] for value in processes.values() if value[1] == group]
    return bool(states), any(value not in {"Z", "X"} for value in states)

def initial_ownership(process, anchor_pid):
    processes = process_table()
    if processes is None:
        return None
    result = set()
    for pid in (process.pid, anchor_pid):
        value = processes.get(pid)
        if value is None or value[1] != process.pid or value[2] in {"Z", "X"}:
            return None
        result.add((pid, value[3]))
    return result

def descendant_identities(ownership):
    processes = process_table()
    if processes is None:
        return None
    return extend_descendant_identities(ownership, processes)

def extend_descendant_identities(ownership, processes):
    result = set(ownership)
    parents = {
        pid for pid, started in ownership
        if pid in processes and processes[pid][3] == started
    }
    while parents:
        children = {
            (pid, value[3]) for pid, value in processes.items()
            if value[0] in parents and (pid, value[3]) not in result
        }
        if not children:
            break
        result.update(children)
        parents = {pid for pid, _ in children}
    return result

def active_identities(ownership):
    processes = process_table()
    if processes is None:
        return None
    return {
        identity for identity in ownership
        if identity[0] in processes
        and processes[identity[0]][3] == identity[1]
        and processes[identity[0]][2] not in {"Z", "X"}
    }

def signal_identities(ownership, requested, missing_ok=True):
    active = active_identities(ownership)
    if active is None:
        return False
    if not missing_ok and active != ownership:
        return False
    success = True
    for pid, _ in active:
        try:
            os.kill(pid, requested)
        except ProcessLookupError:
            if not missing_ok:
                success = False
        except OSError:
            success = False
    return success

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

def wait_quiescent(process, ownership, marker, duration):
    deadline = time.monotonic() + duration
    while True:
        state = group_state(process.pid)
        active = active_identities(ownership) if ownership is not None else set()
        processes = process_table()
        marked = marked_identities(marker, processes) if processes is not None else None
        if (
            state is not None and active is not None and marked is not None
            and not state[1] and not active and not marked
        ):
            return True
        if state is None or active is None or marked is None or time.monotonic() >= deadline:
            return False
        time.sleep(POLL)

def freeze_identities(process, ownership, marker):
    deadline = time.monotonic() + TERM_TIMEOUT
    owned = set(ownership)
    while True:
        processes = process_table()
        if processes is None:
            return owned, False
        marked = marked_identities(marker, processes)
        if marked is None:
            return owned, False
        owned.update(marked)
        owned = extend_descendant_identities(owned, processes)
        group_identities = {
            (pid, value[3]) for pid, value in processes.items()
            if value[1] == process.pid and value[2] not in {"Z", "X"}
        }
        group_owned = group_identities & owned
        if group_owned:
            owned.update(group_identities)
            owned = extend_descendant_identities(owned, processes)
            if not signal_group(process, signal.SIGSTOP):
                return owned, False
        active = active_identities(owned)
        if active is None or not active:
            return owned, False
        if not signal_identities(active, signal.SIGSTOP, missing_ok=True):
            return owned, False
        verified = process_table()
        if verified is None:
            return owned, False
        expanded = extend_descendant_identities(owned, verified)
        marked = marked_identities(marker, verified)
        if marked is None:
            return owned, False
        expanded.update(marked)
        verified_group = {
            (pid, value[3]) for pid, value in verified.items()
            if value[1] == process.pid and value[2] not in {"Z", "X"}
        }
        if verified_group & expanded:
            expanded.update(verified_group)
        if expanded != owned:
            owned = expanded
            continue
        states = [
            value[2] for pid, started in owned
            if pid in verified
            and (value := verified[pid])[3] == started
            and value[2] not in {"Z", "X"}
        ]
        if states and all(state == "T" for state in states):
            return owned, True
        if time.monotonic() >= deadline:
            return owned, False
        time.sleep(POLL)

def kill_identities(process, ownership, marker):
    processes = process_table()
    if processes is None:
        return False
    group_identities = {
        (pid, value[3]) for pid, value in processes.items()
        if value[1] == process.pid and value[2] not in {"Z", "X"}
    }
    success = True
    if group_identities & ownership:
        success = signal_group(process, signal.SIGKILL)
    success = signal_identities(ownership, signal.SIGKILL) and success
    return success and wait_quiescent(process, ownership, marker, KILL_TIMEOUT)

def cleanup(process, marker, ownership=None, tracking_verified=True):
    frozen = ownership is not None
    if ownership is None:
        quiescent = signal_group(process, signal.SIGKILL)
        quiescent = quiescent and wait_quiescent(process, None, marker, KILL_TIMEOUT)
    else:
        ownership, frozen = freeze_identities(process, ownership, marker)
        quiescent = kill_identities(process, ownership, marker)
    try:
        process.wait(timeout=0.5)
    except (OSError, subprocess.SubprocessError):
        return False
    return tracking_verified and frozen and quiescent and process.returncode is not None

def main():
    signal.signal(signal.SIGCHLD, signal.SIG_DFL)
    config_fd, lifeline_fd, status_fd = (int(value) for value in sys.argv[1:4])
    config = read_config(config_fd)
    close_fd(config_fd)
    marker = config.get("ownership_marker")
    if (
        not isinstance(marker, str)
        or len(marker) != 32
        or any(c not in "0123456789abcdef" for c in marker)
    ):
        return 4
    process = None
    ownership = None
    tracking_verified = True
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
            return 2 if cleanup(process, marker) else 4
        if not ready.endswith(b"\n") or not ready[:-1].isdigit():
            raise RuntimeError("child launcher failed")
        anchor_pid = int(ready[:-1])
        if anchor_pid <= 1:
            raise RuntimeError("invalid child anchor")
        ownership = initial_ownership(process, anchor_pid)
        if ownership is None:
            raise RuntimeError("child ownership unavailable")
        close_fd(ready_read)
        os.write(status_fd, f"{process.pid} {anchor_pid}\n".encode("ascii"))
        close_fd(status_fd)
        for descriptor in (0, 1, 2):
            close_fd(descriptor)
        status = None
        parent_gone = False
        while status is None and not parent_gone:
            observed = descendant_identities(ownership)
            if observed is None:
                tracking_verified = False
            else:
                ownership = observed
            status = child_status(process)
            if status is not None:
                break
            readable, _, _ = select.select([lifeline_fd], [], [], POLL)
            if readable:
                parent_gone = os.read(lifeline_fd, 1) == b""
        close_fd(lifeline_fd)
        if not cleanup(process, marker, ownership, tracking_verified):
            return 4
        if parent_gone:
            return 2
        return 0 if status == 0 else 1
    except BaseException:
        if process is not None:
            cleanup(process, marker, ownership, tracking_verified)
        return 4
    finally:
        close_fd(status_fd)
        close_fd(lifeline_fd)

raise SystemExit(main())
"""


class _DarwinBSDInfo(ctypes.Structure):
    _fields_ = [
        ("flags", ctypes.c_uint32),
        ("status", ctypes.c_uint32),
        ("xstatus", ctypes.c_uint32),
        ("pid", ctypes.c_uint32),
        ("ppid", ctypes.c_uint32),
        ("uid", ctypes.c_uint32),
        ("gid", ctypes.c_uint32),
        ("ruid", ctypes.c_uint32),
        ("rgid", ctypes.c_uint32),
        ("svuid", ctypes.c_uint32),
        ("svgid", ctypes.c_uint32),
        ("reserved", ctypes.c_uint32),
        ("command", ctypes.c_char * 16),
        ("name", ctypes.c_char * 32),
        ("nfiles", ctypes.c_uint32),
        ("pgid", ctypes.c_uint32),
        ("pjobc", ctypes.c_uint32),
        ("tdev", ctypes.c_uint32),
        ("tpgid", ctypes.c_uint32),
        ("nice", ctypes.c_int32),
        ("started_seconds", ctypes.c_uint64),
        ("started_microseconds", ctypes.c_uint64),
    ]


def _darwin_process_table() -> ProcessTable | None:
    try:
        library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        count = library.proc_listallpids(None, 0)
        if count <= 0:
            return None
        pids = (ctypes.c_int * (count + 128))()
        count = library.proc_listallpids(pids, ctypes.sizeof(pids))
        if count <= 0:
            return None
        processes: ProcessTable = {}
        for pid in pids[:count]:
            if pid <= 1:
                continue
            info = _DarwinBSDInfo()
            size = library.proc_pidinfo(pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info))
            if size != ctypes.sizeof(info):
                continue
            state = "Z" if info.status == 5 else "T" if info.status == 4 else "R"
            processes[pid] = (
                int(info.ppid),
                int(info.pgid),
                state,
                f"{info.started_seconds}:{info.started_microseconds}",
            )
        return processes
    except (AttributeError, OSError, TypeError, ValueError):
        return None


def process_table() -> ProcessTable | None:
    if sys.platform == "darwin":
        return _darwin_process_table()
    proc = Path("/proc")
    if proc.is_dir():
        processes: ProcessTable = {}
        try:
            entries = proc.iterdir()
            for entry in entries:
                if not entry.name.isdigit():
                    continue
                try:
                    raw = (entry / "stat").read_bytes()
                    fields = raw[raw.rindex(b") ") + 2 :].split()
                    if len(fields) >= 20:
                        processes[int(entry.name)] = (
                            int(fields[1]),
                            int(fields[2]),
                            fields[0][:1].decode("ascii"),
                            fields[19].decode("ascii"),
                        )
                except (FileNotFoundError, ProcessLookupError):
                    continue
                except (OSError, UnicodeError, ValueError):
                    return None
        except OSError:
            return None
        return processes
    try:
        output = subprocess.run(
            ["/bin/ps", "-axo", "pid=,ppid=,pgid=,state=,lstart="],
            check=True,
            capture_output=True,
            text=True,
            timeout=STATE_INSPECTION_TIMEOUT,
            env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C", "TZ": "UTC"},
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    processes = {}
    for line in output.splitlines():
        fields = line.split()
        if len(fields) >= 9 and all(field.isdigit() for field in fields[:3]):
            processes[int(fields[0])] = (
                int(fields[1]),
                int(fields[2]),
                fields[3][:1],
                " ".join(fields[4:]),
            )
    return processes


def marked_identities(
    marker: str, processes: ProcessTable | None = None
) -> set[ProcessIdentity] | None:
    processes = process_table() if processes is None else processes
    if processes is None:
        return None
    needle = f"{OWNERSHIP_ENV}={marker}".encode("ascii")
    if Path("/proc").is_dir():
        result: set[ProcessIdentity] = set()
        for pid, (_, _, state, started) in processes.items():
            if state in {"Z", "X"}:
                continue
            path = Path("/proc") / str(pid)
            try:
                if path.stat().st_uid != os.getuid():
                    continue
                environment = (path / "environ").read_bytes().split(b"\0")
            except (FileNotFoundError, ProcessLookupError):
                continue
            except OSError:
                return None
            if needle in environment:
                result.add((pid, started))
        return result
    try:
        output = subprocess.run(
            ["/bin/ps", "eww", "-axo", "pid=,command="],
            check=True,
            capture_output=True,
            timeout=STATE_INSPECTION_TIMEOUT,
            env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    padded = b" " + needle + b" "
    result = set()
    for line in output.splitlines():
        fields = line.split(maxsplit=1)
        if not fields or not fields[0].isdigit():
            continue
        pid = int(fields[0])
        if (process := processes.get(pid)) is None or process[2] in {"Z", "X"}:
            continue
        haystack = b" " + (fields[1] if len(fields) == 2 else b"") + b" "
        if padded in haystack:
            result.add((pid, process[3]))
    return result


def extend_descendants(
    ownership: set[ProcessIdentity], processes: ProcessTable
) -> set[ProcessIdentity]:
    result = set(ownership)
    parents = {
        pid
        for pid, started in ownership
        if (process := processes.get(pid)) is not None and process[3] == started
    }
    while parents:
        children = {
            (pid, started)
            for pid, (parent, _, _, started) in processes.items()
            if parent in parents and (pid, started) not in result
        }
        if not children:
            return result
        result.update(children)
        parents = {pid for pid, _ in children}
    return result


def group_state(group: int) -> tuple[bool, bool] | None:
    processes = process_table()
    if processes is None:
        return None
    states = [state for _, process_group, state, _ in processes.values() if process_group == group]
    return bool(states), any(state not in {"Z", "X"} for state in states)


def process_identity(pid: int, *, expected_group: int) -> ProcessIdentity | None:
    processes = process_table()
    if processes is None or (process := processes.get(pid)) is None:
        return None
    _, group, state, started = process
    return (pid, started) if group == expected_group and state not in {"Z", "X"} else None


def group_identities(group: int) -> set[ProcessIdentity] | None:
    processes = process_table()
    if processes is None:
        return None
    return {
        (pid, started)
        for pid, (_, process_group, state, started) in processes.items()
        if process_group == group and state not in {"Z", "X"}
    }


def active_identities(ownership: set[ProcessIdentity]) -> set[ProcessIdentity] | None:
    processes = process_table()
    if processes is None:
        return None
    return {
        identity
        for identity in ownership
        if (process := processes.get(identity[0])) is not None
        and process[3] == identity[1]
        and process[2] not in {"Z", "X"}
    }
