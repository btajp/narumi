from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from narumi.errors import CancelledError, EngineUnavailableError
from narumi.providers.claude import transport as transport_module
from narumi.providers.claude.protocol import WorkerRequest
from narumi.providers.claude.transport import (
    _WATCHDOG_PROGRAM,
    LIFELINE_ENV,
    SubprocessWorkerRunner,
    _group_state,
)

CONNECTION = "conn-0123456789abcdef"
KEY = "synthetic-claude-sdk-transport-key-61805"
MODEL = "claude-fixture-1-20260901"
PROMPT = "Synthetic transport-only transcript"
RUNTIME = {
    "resource_id": "claude-agent-sdk-0-2-144",
    "sdk_version": "0.2.144",
    "cli_version": "2.1.239",
    "cli_sha256": "a" * 64,
    "sdk_source_sha256": "b" * 64,
    "isolation_profile_sha256": "c" * 64,
}

SUCCESS_WORKER = r"""
import json, sys
request = json.loads(sys.stdin.buffer.read())
response = {
    "protocol_version": 1,
    "status": "ok",
    "text": "Fixture minutes",
    "returned_model": request["model_id"],
    "usage": {"input_tokens": 11, "output_tokens": 4},
    "runtime_evidence": request["expected_runtime"],
}
sys.stdout.write(json.dumps(response, separators=(",", ":")) + "\n")
"""


def request():
    return WorkerRequest(CONNECTION, KEY, MODEL, PROMPT, "Synthetic instructions", RUNTIME)


def test_private_pipe_is_the_only_secret_and_prompt_transport(tmp_path, monkeypatch):
    processes = []
    configs = []
    real_popen = subprocess.Popen
    real_write_descriptor = transport_module._write_descriptor

    def observed(command, **kwargs):
        if kwargs.get("start_new_session") is not True:
            return real_popen(command, **kwargs)
        assert KEY not in repr(command) and PROMPT not in repr(command)
        assert KEY not in repr(kwargs["env"]) and PROMPT not in repr(kwargs["env"])
        assert kwargs["start_new_session"] is True and kwargs["close_fds"] is True
        assert kwargs["stderr"] is subprocess.DEVNULL and kwargs["umask"] == 0o077
        if os.name == "posix":
            assert kwargs["env"][LIFELINE_ENV].isdigit()
            assert int(kwargs["env"][LIFELINE_ENV]) in kwargs["pass_fds"]
            assert kwargs["stdin"] is subprocess.DEVNULL
            assert kwargs["stdout"] is subprocess.DEVNULL
        process = real_popen(command, **kwargs)
        processes.append(process)
        return process

    def observed_config(descriptor, payload):
        assert KEY.encode() not in payload and PROMPT.encode() not in payload
        configs.append(payload)
        return real_write_descriptor(descriptor, payload)

    monkeypatch.setattr("narumi.providers.claude.transport.subprocess.Popen", observed)
    monkeypatch.setattr(transport_module, "_write_descriptor", observed_config)
    runner = SubprocessWorkerRunner((sys.executable, "-I", "-c", SUCCESS_WORKER))
    result = runner(
        request(),
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)},
        cwd=tmp_path,
        should_cancel=lambda: False,
        timeout=3,
    )
    assert result.text == "Fixture minutes" and result.returned_model == MODEL
    assert len(processes) == 1 and processes[0].poll() == 0
    assert len(configs) == 1
    config = json.loads(configs[0])
    assert LIFELINE_ENV not in config["environment"]


def test_pre_submission_cancellation_never_starts_a_process(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "narumi.providers.claude.transport.subprocess.Popen",
        lambda *args, **kwargs: pytest.fail("worker must not start"),
    )
    runner = SubprocessWorkerRunner((sys.executable, "-I", "-c", SUCCESS_WORKER))
    with pytest.raises(CancelledError) as failure:
        runner(request(), env={}, cwd=tmp_path, should_cancel=lambda: True, timeout=3)
    assert failure.value.details == {"outcome_unknown": False}


@pytest.mark.skipif(os.name != "posix", reason="POSIX waitid identity guarantee")
def test_watchdog_rechecks_waitid_when_getpgid_loses_just_exited_child():
    namespace = {}
    exec(_WATCHDOG_PROGRAM.rsplit("raise SystemExit(main())", 1)[0], namespace)
    statuses = [
        None,
        SimpleNamespace(si_pid=41, si_code=os.CLD_EXITED, si_status=0),
    ]

    class FakeOS:
        P_PID = os.P_PID
        WEXITED = os.WEXITED
        WNOHANG = os.WNOHANG
        WNOWAIT = os.WNOWAIT
        CLD_EXITED = os.CLD_EXITED
        CLD_KILLED = os.CLD_KILLED
        CLD_DUMPED = os.CLD_DUMPED

        @staticmethod
        def waitid(*_):
            return statuses.pop(0)

        @staticmethod
        def getpgid(_):
            raise ProcessLookupError

    namespace["os"] = FakeOS
    assert namespace["reserved"](SimpleNamespace(pid=41)) is True
    assert statuses == []


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group guarantee")
def test_watchdog_accepts_darwin_zombie_only_eperm_as_quiescent():
    namespace = {}
    exec(_WATCHDOG_PROGRAM.rsplit("raise SystemExit(main())", 1)[0], namespace)
    namespace["reserved"] = lambda _: True
    namespace["group_state"] = lambda _: (True, False)

    class FakeOS:
        @staticmethod
        def killpg(*_):
            raise PermissionError

    namespace["os"] = FakeOS
    assert namespace["signal_group"](SimpleNamespace(pid=41), signal.SIGTERM) is True


def test_worker_failure_after_submission_is_unknown_and_not_retried(tmp_path, monkeypatch):
    count = 0
    real_popen = subprocess.Popen

    def observed(*args, **kwargs):
        nonlocal count
        if kwargs.get("start_new_session") is True:
            count += 1
        return real_popen(*args, **kwargs)

    monkeypatch.setattr("narumi.providers.claude.transport.subprocess.Popen", observed)
    script = "import sys; sys.stdin.buffer.read(); raise SystemExit(2)"
    runner = SubprocessWorkerRunner((sys.executable, "-I", "-c", script))
    with pytest.raises(EngineUnavailableError) as failure:
        runner(request(), env={}, cwd=tmp_path, should_cancel=lambda: False, timeout=3)
    assert failure.value.details == {
        "reason": "provider_generation_outcome_unknown",
        "outcome_unknown": True,
    }
    assert count == 1


def test_cancellation_after_submission_terminates_the_owned_process_group(tmp_path, monkeypatch):
    processes = []
    real_popen = subprocess.Popen

    def observed(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        if kwargs.get("start_new_session") is True:
            processes.append(process)
        return process

    monkeypatch.setattr("narumi.providers.claude.transport.subprocess.Popen", observed)
    received = tmp_path / "request.received"
    script = (
        "import pathlib,sys,time; sys.stdin.buffer.read(); "
        f"pathlib.Path({str(received)!r}).write_text('received'); time.sleep(30)"
    )
    runner = SubprocessWorkerRunner((sys.executable, "-I", "-c", script))
    with pytest.raises(CancelledError) as failure:
        runner(
            request(),
            env={},
            cwd=tmp_path,
            should_cancel=received.exists,
            timeout=3,
        )
    assert failure.value.details == {"outcome_unknown": True}
    assert len(processes) == 1 and processes[0].poll() is not None
    with pytest.raises(ProcessLookupError):
        os.kill(processes[0].pid, 0)


def test_timeout_after_submission_is_unknown_and_kills_stubborn_worker(tmp_path):
    script = (
        "import signal,sys,time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "sys.stdin.buffer.read(); time.sleep(30)"
    )
    runner = SubprocessWorkerRunner((sys.executable, "-I", "-c", script))
    with pytest.raises(EngineUnavailableError) as failure:
        runner(request(), env={}, cwd=tmp_path, should_cancel=lambda: False, timeout=0.15)
    assert failure.value.details["outcome_unknown"] is True


def test_spawn_failure_is_known_before_submission(tmp_path):
    runner = SubprocessWorkerRunner((str(Path(tmp_path) / "missing-worker"),))
    with pytest.raises(EngineUnavailableError) as failure:
        runner(request(), env={}, cwd=tmp_path, should_cancel=lambda: False, timeout=1)
    assert failure.value.details == {
        "reason": "claude_sdk_worker_unavailable",
        "outcome_unknown": False,
    }


@pytest.mark.skipif(os.name != "posix", reason="POSIX process identity guarantee")
def test_external_watchdog_parents_worker_and_reserves_exited_leader(tmp_path):
    identity = tmp_path / "worker.identity"
    reserved = tmp_path / "leader.reserved"
    child = f"""
import os, signal, time
leader = os.getppid()
def stop(*_):
    try:
        os.kill(leader, 0)
    except OSError:
        os._exit(2)
    open({str(reserved)!r}, "w").write("reserved")
    os._exit(0)
signal.signal(signal.SIGTERM, stop)
while True:
    time.sleep(30)
"""
    worker = f"""
import json, os, subprocess, sys, time
request = json.loads(sys.stdin.buffer.read())
child = subprocess.Popen([sys.executable, "-c", {child!r}], close_fds=True)
open({str(identity)!r}, "w").write(f"{{os.getpid()}} {{os.getpgrp()}} {{os.getppid()}}")
time.sleep(0.1)
response = {{
    "protocol_version": 1,
    "status": "ok",
    "text": "Fixture minutes",
    "returned_model": request["model_id"],
    "usage": {{"input_tokens": 11, "output_tokens": 4}},
    "runtime_evidence": request["expected_runtime"],
}}
sys.stdout.write(json.dumps(response, separators=(",", ":")) + "\\n")
sys.stdout.flush()
"""
    runner = SubprocessWorkerRunner((sys.executable, "-I", "-c", worker))
    result = runner(
        request(),
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)},
        cwd=tmp_path,
        should_cancel=lambda: False,
        timeout=3,
    )
    assert result.text == "Fixture minutes"
    worker_pid, group, watchdog_pid = (int(value) for value in identity.read_text().split())
    assert worker_pid == group and watchdog_pid != group
    assert reserved.read_text() == "reserved"


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group guarantee")
def test_success_waits_until_descendant_process_group_is_gone(tmp_path):
    child_pid_file = tmp_path / "child.pid"
    child = "import os,time; os.close(0); os.close(1); os.close(2); time.sleep(30)"
    script = f"""
import json, os, subprocess, sys
request = json.loads(sys.stdin.buffer.read())
child = subprocess.Popen([sys.executable, "-c", {child!r}], close_fds=True)
open({str(child_pid_file)!r}, "w").write(f"{{child.pid}} {{os.getpgrp()}}")
response = {{
    "protocol_version": 1,
    "status": "ok",
    "text": "Fixture minutes",
    "returned_model": request["model_id"],
    "usage": {{"input_tokens": 11, "output_tokens": 4}},
    "runtime_evidence": request["expected_runtime"],
}}
sys.stdout.write(json.dumps(response, separators=(",", ":")) + "\\n")
sys.stdout.flush()
"""
    runner = SubprocessWorkerRunner((sys.executable, "-I", "-c", script))
    result = runner(request(), env={}, cwd=tmp_path, should_cancel=lambda: False, timeout=3)
    assert result.text == "Fixture minutes"
    child_pid, group = (int(value) for value in child_pid_file.read_text().split())
    state = _group_state(group)
    assert state is not None and state[1] is False
    try:
        os.kill(child_pid, 0)
    except ProcessLookupError:
        pass


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group guarantee")
def test_cleanup_gives_descendant_a_bounded_term_grace_before_kill(tmp_path):
    ready = tmp_path / "child.ready"
    completed = tmp_path / "child.completed"
    child = f"""
import os, signal, time
for descriptor in (0, 1, 2):
    try:
        os.close(descriptor)
    except OSError:
        pass
def stop(*_):
    time.sleep(0.6)
    open({str(completed)!r}, "w").write("terminated")
    os._exit(0)
signal.signal(signal.SIGTERM, stop)
open({str(ready)!r}, "w").write("ready")
while True:
    time.sleep(30)
"""
    script = f"""
import json, subprocess, sys, time
request = json.loads(sys.stdin.buffer.read())
subprocess.Popen([sys.executable, "-c", {child!r}], close_fds=True)
deadline = time.monotonic() + 2
while not __import__("pathlib").Path({str(ready)!r}).exists():
    if time.monotonic() >= deadline:
        raise SystemExit(2)
    time.sleep(0.01)
response = {{
    "protocol_version": 1,
    "status": "ok",
    "text": "Fixture minutes",
    "returned_model": request["model_id"],
    "usage": {{"input_tokens": 11, "output_tokens": 4}},
    "runtime_evidence": request["expected_runtime"],
}}
sys.stdout.write(json.dumps(response, separators=(",", ":")) + "\\n")
sys.stdout.flush()
"""
    runner = SubprocessWorkerRunner((sys.executable, "-I", "-c", script))
    result = runner(request(), env={}, cwd=tmp_path, should_cancel=lambda: False, timeout=3)
    assert result.text == "Fixture minutes"
    assert completed.read_text() == "terminated"


def test_cleanup_failure_after_submission_is_unknown(tmp_path, monkeypatch):
    target = (
        "narumi.providers.claude.transport._terminate_supervised"
        if os.name == "posix"
        else "narumi.providers.claude.transport._terminate"
    )
    monkeypatch.setattr(target, lambda process: False)
    runner = SubprocessWorkerRunner((sys.executable, "-I", "-c", SUCCESS_WORKER))
    with pytest.raises(EngineUnavailableError) as failure:
        runner(request(), env={}, cwd=tmp_path, should_cancel=lambda: False, timeout=3)
    assert failure.value.details == {
        "reason": "provider_generation_outcome_unknown",
        "outcome_unknown": True,
    }


@pytest.mark.skipif(os.name != "posix", reason="POSIX parent-death lifeline")
def test_server_sigkill_watchdog_kills_term_ignoring_worker_group(tmp_path):
    pid_file = tmp_path / "lifeline.pid"
    ready = tmp_path / "stubborn.ready"
    child = f"""
import os, signal, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
open({str(ready)!r}, "w").write("ready")
for descriptor in (0, 1, 2):
    try:
        os.close(descriptor)
    except OSError:
        pass
while True:
    time.sleep(30)
"""
    worker = f"""
import os, subprocess, sys, time
sys.stdin.buffer.read()
signal = __import__("signal")
signal.signal(signal.SIGTERM, signal.SIG_IGN)
child = subprocess.Popen([sys.executable, "-c", {child!r}], close_fds=True)
deadline = time.monotonic() + 2
while not __import__("pathlib").Path({str(ready)!r}).exists():
    if time.monotonic() >= deadline:
        raise SystemExit(2)
    time.sleep(0.01)
open({str(pid_file)!r}, "w").write(f"{{os.getpid()}} {{os.getpgrp()}} {{child.pid}}")
time.sleep(30)
"""
    wrapper = f"""
import sys
from pathlib import Path
from narumi.providers.claude.protocol import WorkerRequest
from narumi.providers.claude.transport import SubprocessWorkerRunner
request = WorkerRequest(
    {CONNECTION!r}, {KEY!r}, {MODEL!r}, {PROMPT!r}, None, {RUNTIME!r}
)
SubprocessWorkerRunner((sys.executable, "-I", "-c", {worker!r}))(
    request,
    env={{}},
    cwd=Path({str(tmp_path)!r}),
    should_cancel=lambda: False,
    timeout=30,
)
"""
    parent = subprocess.Popen(
        (sys.executable, "-I", "-c", wrapper),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )
    deadline = time.monotonic() + 3
    while not pid_file.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert pid_file.exists()
    worker_pid, group, _ = (int(value) for value in pid_file.read_text().split())
    assert worker_pid == group
    os.kill(parent.pid, signal.SIGKILL)
    parent.wait(timeout=3)
    _assert_group_quiescent(group)


@pytest.mark.skipif(os.name != "posix", reason="POSIX worker-death watchdog")
def test_worker_sigkill_watchdog_kills_term_ignoring_descendant(tmp_path):
    pid_file = tmp_path / "worker-death.pid"
    ready = tmp_path / "worker-death.ready"
    child = f"""
import os, signal, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
open({str(ready)!r}, "w").write("ready")
for descriptor in (0, 1, 2):
    try:
        os.close(descriptor)
    except OSError:
        pass
while True:
    time.sleep(30)
"""
    worker = f"""
import os, subprocess, sys, time
sys.stdin.buffer.read()
child = subprocess.Popen([sys.executable, "-c", {child!r}], close_fds=True)
deadline = time.monotonic() + 2
while not __import__("pathlib").Path({str(ready)!r}).exists():
    if time.monotonic() >= deadline:
        raise SystemExit(2)
    time.sleep(0.01)
open({str(pid_file)!r}, "w").write(f"{{os.getpid()}} {{os.getpgrp()}} {{child.pid}}")
time.sleep(30)
"""
    wrapper = f"""
import sys
from pathlib import Path
from narumi.providers.claude.protocol import WorkerRequest
from narumi.providers.claude.transport import SubprocessWorkerRunner
request = WorkerRequest(
    {CONNECTION!r}, {KEY!r}, {MODEL!r}, {PROMPT!r}, None, {RUNTIME!r}
)
SubprocessWorkerRunner((sys.executable, "-I", "-c", {worker!r}))(
    request,
    env={{}},
    cwd=Path({str(tmp_path)!r}),
    should_cancel=lambda: False,
    timeout=30,
)
"""
    server = subprocess.Popen(
        (sys.executable, "-I", "-c", wrapper),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )
    deadline = time.monotonic() + 3
    while not pid_file.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert pid_file.exists()
    worker_pid, group, _ = (int(value) for value in pid_file.read_text().split())
    assert worker_pid == group
    os.kill(worker_pid, signal.SIGKILL)
    server.wait(timeout=5)
    _assert_group_quiescent(group)


@pytest.mark.skipif(os.name != "posix", reason="POSIX watchdog failure recovery")
def test_live_server_recovers_worker_group_after_watchdog_sigkill(tmp_path):
    pid_file = tmp_path / "watchdog-death.pid"
    ready = tmp_path / "watchdog-death.ready"
    child = f"""
import os, signal, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
open({str(ready)!r}, "w").write("ready")
for descriptor in (0, 1, 2):
    try:
        os.close(descriptor)
    except OSError:
        pass
while True:
    time.sleep(30)
"""
    worker = f"""
import os, signal, subprocess, sys, time
sys.stdin.buffer.read()
signal.signal(signal.SIGTERM, signal.SIG_IGN)
child = subprocess.Popen([sys.executable, "-c", {child!r}], close_fds=True)
deadline = time.monotonic() + 2
while not __import__("pathlib").Path({str(ready)!r}).exists():
    if time.monotonic() >= deadline:
        raise SystemExit(2)
    time.sleep(0.01)
open({str(pid_file)!r}, "w").write(
    f"{{os.getpid()}} {{os.getpgrp()}} {{os.getppid()}} {{child.pid}}"
)
time.sleep(30)
"""
    wrapper = f"""
import sys
from pathlib import Path
from narumi.providers.claude.protocol import WorkerRequest
from narumi.providers.claude.transport import SubprocessWorkerRunner
request = WorkerRequest(
    {CONNECTION!r}, {KEY!r}, {MODEL!r}, {PROMPT!r}, None, {RUNTIME!r}
)
SubprocessWorkerRunner((sys.executable, "-I", "-c", {worker!r}))(
    request,
    env={{}},
    cwd=Path({str(tmp_path)!r}),
    should_cancel=lambda: False,
    timeout=30,
)
"""
    server = subprocess.Popen(
        (sys.executable, "-I", "-c", wrapper),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )
    deadline = time.monotonic() + 3
    while not pid_file.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert pid_file.exists()
    worker_pid, group, watchdog_pid, _ = (int(value) for value in pid_file.read_text().split())
    assert worker_pid == group and watchdog_pid not in {worker_pid, server.pid}
    os.kill(watchdog_pid, signal.SIGKILL)
    server.wait(timeout=6)
    _assert_group_quiescent(group)


def _assert_group_quiescent(group: int) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        state = _group_state(group)
        if state is not None and not state[1]:
            return
        time.sleep(0.02)
    try:
        os.killpg(group, signal.SIGKILL)
    except ProcessLookupError:
        pass
    pytest.fail("worker process group survived watchdog cleanup")
