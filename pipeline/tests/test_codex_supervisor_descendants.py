from __future__ import annotations

import json
import os
import selectors
import signal
import sys
import time

import pytest
from narumi.providers.codex import _supervisor


def _identity(pid: int) -> tuple[int, str]:
    processes = _supervisor._process_table()
    assert processes is not None
    assert (process := processes.get(pid)) is not None
    return pid, process[3]


def _wait_inactive(identity: tuple[int, str]) -> None:
    deadline = time.monotonic() + 3
    while True:
        active = _supervisor._active_identities({identity})
        assert active is not None
        if not active:
            return
        assert time.monotonic() < deadline
        time.sleep(0.01)


def _read_pid(process: _supervisor.SupervisedProcess) -> int:
    deadline = time.monotonic() + 3
    output = bytearray()
    with selectors.DefaultSelector() as selector:
        _supervisor._lease_register(selector, process.stdout_lease, selectors.EVENT_READ)
        while b"\n" not in output:
            remaining = deadline - time.monotonic()
            assert remaining > 0
            assert selector.select(remaining)
            block = _supervisor._lease_read(process.stdout_lease, 32)
            assert block
            output.extend(block)
            assert len(output) <= 32
    line, remainder = bytes(output).split(b"\n", 1)
    assert not remainder
    assert line.isdigit()
    return int(line)


@pytest.mark.skipif(os.name != "posix", reason="POSIX descendant supervision")
def test_terminate_reaps_descendant_that_created_its_own_process_group(tmp_path):
    replacement_path = tmp_path / "term-replacement.pid"
    child = """
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

def replace_on_term(*_):
    replacement = subprocess.Popen(
        [sys.executable, "-I", "-S", "-c", "import time; time.sleep(30)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    Path("term-replacement.pid").write_text(str(replacement.pid))
    os._exit(0)

signal.signal(signal.SIGTERM, replace_on_term)
time.sleep(30)
"""
    server = f"""
import subprocess
import sys

descendant = subprocess.Popen(
    [sys.executable, "-I", "-S", "-c", {child!r}],
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
    start_new_session=True,
)
sys.stdout.write(str(descendant.pid) + "\\n")
sys.stdout.flush()
sys.stdin.buffer.read()
"""
    process = _supervisor.start([sys.executable, "-I", "-S", "-c", server], {}, tmp_path)
    descendant_identity: tuple[int, str] | None = None
    try:
        descendant_pid = _read_pid(process)
        descendant_identity = _identity(descendant_pid)
        assert os.getpgid(descendant_pid) == descendant_pid
        assert _supervisor.terminate(process) is True
        _wait_inactive(descendant_identity)
        assert not replacement_path.exists()
    finally:
        if descendant_identity is not None:
            active = _supervisor._active_identities({descendant_identity})
            if active:
                os.kill(descendant_identity[0], signal.SIGKILL)
                _wait_inactive(descendant_identity)
        if replacement_path.exists():
            replacement_pid = int(replacement_path.read_text())
            try:
                replacement_identity = _identity(replacement_pid)
            except AssertionError:
                pass
            else:
                os.kill(replacement_pid, signal.SIGKILL)
                _wait_inactive(replacement_identity)


@pytest.mark.skipif(os.name != "posix", reason="POSIX parent-death supervision")
def test_watchdog_reaps_escaped_descendant_after_client_process_dies(tmp_path):
    replacement_path = tmp_path / "parent-death-replacement.pid"
    report_read, report_write = os.pipe()
    owner = os.fork()
    if owner == 0:
        os.close(report_read)
        try:
            child = """
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

def replace_on_term(*_):
    replacement = subprocess.Popen(
        [sys.executable, "-I", "-S", "-c", "import time; time.sleep(30)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    Path("parent-death-replacement.pid").write_text(str(replacement.pid))
    os._exit(0)

signal.signal(signal.SIGTERM, replace_on_term)
time.sleep(30)
"""
            server = f"""
import subprocess
import sys

descendant = subprocess.Popen(
    [sys.executable, "-I", "-S", "-c", {child!r}],
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
    start_new_session=True,
)
sys.stdout.write(str(descendant.pid) + "\\n")
sys.stdout.flush()
sys.stdin.buffer.read()
"""
            process = _supervisor.start([sys.executable, "-I", "-S", "-c", server], {}, tmp_path)
            identity = _identity(_read_pid(process))
            os.write(report_write, json.dumps(identity).encode("ascii"))
        except BaseException:
            os._exit(1)
        finally:
            os.close(report_write)
        os._exit(0)

    os.close(report_write)
    encoded = bytearray()
    while chunk := os.read(report_read, 4096):
        encoded.extend(chunk)
    os.close(report_read)
    _, status = os.waitpid(owner, 0)
    assert os.waitstatus_to_exitcode(status) == 0
    descendant_identity = tuple(json.loads(encoded))
    try:
        _wait_inactive(descendant_identity)
        assert not replacement_path.exists()
    finally:
        active = _supervisor._active_identities({descendant_identity})
        if active:
            os.kill(descendant_identity[0], signal.SIGKILL)
            _wait_inactive(descendant_identity)
        if replacement_path.exists():
            replacement_pid = int(replacement_path.read_text())
            try:
                replacement_identity = _identity(replacement_pid)
            except AssertionError:
                pass
            else:
                os.kill(replacement_pid, signal.SIGKILL)
                _wait_inactive(replacement_identity)


@pytest.mark.skipif(os.name != "posix", reason="POSIX cleanup verification")
def test_killed_watchdog_cannot_claim_untracked_reparented_descendant_was_reaped(tmp_path):
    child = "import time; time.sleep(30)"
    server = f"""
import subprocess
import sys

sys.stdin.buffer.read(1)
descendant = subprocess.Popen(
    [sys.executable, "-I", "-S", "-c", {child!r}],
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
    start_new_session=True,
)
sys.stdout.write(str(descendant.pid) + "\\n")
sys.stdout.flush()
"""
    process = _supervisor.start([sys.executable, "-I", "-S", "-c", server], {}, tmp_path)
    descendant_identity: tuple[int, str] | None = None
    try:
        os.kill(process.watchdog.pid, signal.SIGKILL)
        process.watchdog.wait(timeout=3)
        process.stdin.write(b"x")
        descendant_pid = _read_pid(process)
        descendant_identity = _identity(descendant_pid)
        deadline = time.monotonic() + 3
        while True:
            active = _supervisor._active_identities({process.identity})
            assert active is not None
            if not active:
                break
            assert time.monotonic() < deadline
            time.sleep(0.01)
        processes = _supervisor._process_table()
        assert processes is not None
        assert processes[descendant_pid][0] != process.pid
        assert _supervisor.terminate(process) is False
        _wait_inactive(descendant_identity)
    finally:
        if descendant_identity is not None:
            active = _supervisor._active_identities({descendant_identity})
            if active:
                os.kill(descendant_identity[0], signal.SIGKILL)
                _wait_inactive(descendant_identity)
