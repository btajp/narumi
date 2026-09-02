"""Exercise private Codex pipes using only controlled local Python children."""

from __future__ import annotations

import gc
import json
import os
import select
import signal
import stat
import subprocess
import sys
import textwrap
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
from narumi.errors import CancelledError, EngineUnavailableError
from narumi.providers.codex import _rpc
from narumi.providers.codex._rpc import StdioRPC

SECRET = "fixture-codex-private-value-91374"
PREAMBLE = """
import json, os, sys, time
from pathlib import Path

def read():
    return json.loads(sys.stdin.readline())

def send(value):
    print(json.dumps(value), flush=True)
"""


@pytest.fixture
def rpc_factory(tmp_path):
    processes, transports = [], []
    real_killpg = os.killpg

    def create(script, *, env=None, **kwargs):
        command = [sys.executable, "-I", "-u", "-c", PREAMBLE + textwrap.dedent(script)]
        rpc = StdioRPC(command, env=env or {}, cwd=tmp_path, timeout=3, **kwargs)
        assert rpc._process is not None
        processes.append(rpc._process)
        transports.append(rpc)
        return rpc

    create.processes = processes
    yield create
    try:
        for rpc in transports:
            rpc.close()
    finally:
        # A failing lifecycle assertion must not leave a fixture descendant alive.
        # Every process above was launched in its own fresh, test-owned session.
        for process in processes:
            if process.poll() is None:
                try:
                    real_killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                except PermissionError:
                    # Darwin can report EPERM for a group with only a zombie.
                    if process.poll() is None:
                        process.kill()
                if process.poll() is None:
                    process.kill()
            process.wait(timeout=6)
            for stream in (process.stdin, process.stdout):
                if stream is not None:
                    stream.close()


def assert_private_failure(error, reason):
    assert error.details == {"reason": reason}
    assert SECRET not in json.dumps(error.to_payload())
    assert SECRET not in "".join(traceback.format_exception(error))


def wait_for_file(path):
    deadline = time.monotonic() + 3
    while not path.exists():
        assert time.monotonic() < deadline, "fixture child did not receive its request"
        threading.Event().wait(0.01)


def wait_for_group_quiescent(group, timeout=6):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = _rpc._supervisor._group_state(group)
        if state is not None and not state[1]:
            return
        threading.Event().wait(0.02)
    pytest.fail(f"process group {group} still has live members")


def _descriptor_available(descriptor):
    try:
        os.fstat(descriptor)
    except OSError:
        return False
    return True


def test_round_trip_uses_private_nonblocking_pipes_and_only_supplied_environment(
    rpc_factory, monkeypatch, tmp_path
):
    monkeypatch.setenv("NARUMI_RPC_PARENT_SECRET", SECRET)
    rpc = rpc_factory(
        """
        while True:
            request = read()
            send({"id": request["id"], "result": {
                "request": request,
                "cwd": os.getcwd(),
                "explicit": os.environ.get("LANG"),
                "inherited": "NARUMI_RPC_PARENT_SECRET" in os.environ,
            }})
        """,
        env={"LANG": "fixture value"},
    )
    process = rpc_factory.processes[0]
    assert not os.get_blocking(process.stdin.fileno())
    assert not os.get_blocking(process.stdout.fileno())
    assert os.getpgid(process.pid) == process.pid
    first = rpc.call("initialize", {"clientInfo": {"name": "fixture"}})
    second = rpc.call("model/list", {})
    assert first["request"]["method"] == "initialize"
    assert first["request"]["params"] == {"clientInfo": {"name": "fixture"}}
    assert type(first["request"]["id"]) is int and first["request"]["id"] > 0
    assert second["request"]["id"] != first["request"]["id"]
    assert first["cwd"] == str(tmp_path)
    assert first["explicit"] == "fixture value" and first["inherited"] is False


def test_watchdog_never_inherits_rpc_or_credential_descriptors(rpc_factory, monkeypatch):
    launches = []
    real_popen = subprocess.Popen

    def observe(command, **kwargs):
        inherited = {
            (metadata.st_dev, metadata.st_ino, metadata.st_mode)
            for descriptor in kwargs.get("pass_fds", ())
            if (metadata := os.fstat(descriptor))
        }
        launches.append((command, kwargs, inherited))
        return real_popen(command, **kwargs)

    monkeypatch.setattr(_rpc._supervisor.subprocess, "Popen", observe)
    rpc = rpc_factory(
        "request = read(); send({'id': request['id'], 'result': {}}); sys.stdin.read()"
    )
    assert rpc.call("fixture/check", {}) == {}
    watchdog_launches = [
        item for item in launches if item[0][:4] == (sys.executable, "-I", "-S", "-c")
    ]
    assert len(watchdog_launches) == 1
    command, launch, inherited = watchdog_launches[0]
    assert command[:4] == (sys.executable, "-I", "-S", "-c")
    assert launch["env"] == {
        "PATH": "/usr/bin:/bin",
        "LANG": "C",
        "LC_ALL": "C",
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    assert len(launch["pass_fds"]) == 3
    assert rpc._process is not None
    for stream in (rpc._process.stdin, rpc._process.stdout):
        metadata = os.fstat(stream.fileno())
        assert (metadata.st_dev, metadata.st_ino, metadata.st_mode) not in inherited


def test_api_key_environment_is_rejected_before_watchdog_spawn(monkeypatch, tmp_path):
    launches = []
    monkeypatch.setattr(
        _rpc._supervisor.subprocess,
        "Popen",
        lambda *_args, **_kwargs: launches.append(True),
    )
    with pytest.raises(EngineUnavailableError) as failure:
        StdioRPC(
            [sys.executable, "-I", "-c", "pass"],
            env={"OPENAI_API_KEY": SECRET},
            cwd=tmp_path,
        )
    assert_private_failure(failure.value, "codex_process_unavailable")
    assert launches == []


def test_notifications_arriving_before_response_are_available_to_wait_for(rpc_factory):
    rpc = rpc_factory(
        """
        request = read()
        send({"method": "turn/started", "params": {"id": "fixture-turn"}})
        send({"method": "turn/completed", "params": {"id": "fixture-turn"}})
        send({"id": request["id"], "result": {"accepted": True}})
        sys.stdin.read()
        """
    )
    assert rpc.call("turn/start", {}) == {"accepted": True}
    event = rpc.wait_for(lambda message: message["method"] == "turn/completed")
    assert event == {"method": "turn/completed", "params": {"id": "fixture-turn"}}


@pytest.mark.parametrize("method", ["configWarning", "warning", "model/rerouted", "modelRerouted"])
def test_policy_warnings_abort_preflight_before_a_later_prompt_is_sent(rpc_factory, method):
    rpc = rpc_factory(
        f"""
        request = read()
        send({{"method": {method!r}, "params": {{"message": {SECRET!r}}}}})
        send({{"id": request["id"], "result": {{"config": {{}}}}}})
        sys.stdin.read()
        """
    )
    sent = []
    with pytest.raises(EngineUnavailableError) as failure:
        rpc.call("config/read", {})
        rpc.call("turn/start", {"input": []}, on_sent=lambda: sent.append("turn"))
    assert_private_failure(failure.value, "codex_runtime_configuration_changed")
    assert sent == []


def test_notify_does_not_allocate_request_id_or_expect_reply(rpc_factory):
    rpc = rpc_factory(
        """
        notification, request = read(), read()
        send({"id": request["id"], "result": {"notification": notification}})
        """
    )
    rpc.notify("initialized")
    assert rpc.call("account/read", {}) == {"notification": {"method": "initialized"}}


def test_parallel_calls_are_serialized_before_reverse_responses_can_cross(rpc_factory):
    rpc = rpc_factory(
        """
        import select
        def raw_request():
            line = bytearray()
            while not line.endswith(b"\\n"):
                block = os.read(0, 1)
                if not block:
                    raise EOFError
                line.extend(block)
            return json.loads(line)

        first = raw_request()
        if select.select([0], [], [], 0.2)[0]:
            second = raw_request()
            send({"id": second["id"], "result": {"method": second["method"]}})
            send({"id": first["id"], "result": {"method": first["method"]}})
        else:
            send({"id": first["id"], "result": {"method": first["method"]}})
            second = raw_request()
            send({"id": second["id"], "result": {"method": second["method"]}})
        sys.stdin.read()
        """
    )
    barrier = threading.Barrier(3)

    def invoke(method):
        barrier.wait()
        return rpc.call(method, {}, timeout=3)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(invoke, "fixture/first")
        second = executor.submit(invoke, "fixture/second")
        barrier.wait()
        assert {first.result(timeout=4)["method"], second.result(timeout=4)["method"]} == {
            "fixture/first",
            "fixture/second",
        }


def test_parallel_large_notify_and_call_cannot_interleave_json_lines(rpc_factory):
    rpc = rpc_factory(
        """
        for _ in range(2):
            request = read()
            if "id" in request:
                send({"id": request["id"], "result": {
                    "method": request["method"],
                    "size": len(request["params"]["text"]),
                }})
        sys.stdin.read()
        """
    )
    barrier = threading.Barrier(3)

    def notify():
        barrier.wait()
        rpc.notify("fixture/notify", {"text": "n" * 500_000})

    def call():
        barrier.wait()
        return rpc.call("fixture/call", {"text": "c" * 500_000}, timeout=5)

    with ThreadPoolExecutor(max_workers=2) as executor:
        notification = executor.submit(notify)
        request = executor.submit(call)
        barrier.wait()
        notification.result(timeout=6)
        assert request.result(timeout=6) == {"method": "fixture/call", "size": 500_000}


def test_on_sent_runs_once_before_first_write_even_for_partial_writes(rpc_factory, monkeypatch):
    rpc = rpc_factory(
        """
        request = read()
        send({"id": request["id"], "result": {"size": len(request["params"]["text"])}})
        """
    )
    descriptor = rpc_factory.processes[0].stdin.fileno()
    callbacks, writes = [], []
    real_write = os.write

    def observe_write(fd, data):
        if fd == descriptor:
            assert callbacks == ["sent"]
            data = data[:1024]
            writes.append(len(data))
        return real_write(fd, data)

    monkeypatch.setattr(_rpc.os, "write", observe_write)
    assert rpc.call(
        "turn/start", {"text": "x" * 100_000}, on_sent=lambda: callbacks.append("sent")
    ) == {"size": 100_000}
    assert callbacks == ["sent"] and len(writes) > 1


@pytest.mark.parametrize("identifier", ["True", "1.0", '"1"', "None", "request['id'] + 1"])
def test_response_id_requires_exact_integer_match(rpc_factory, identifier):
    rpc = rpc_factory(
        f"""
        request = read()
        send({{"id": {identifier}, "result": {{"private": {SECRET!r}}}}})
        """
    )
    with pytest.raises(EngineUnavailableError) as failure:
        rpc.call("account/read", {})
    assert_private_failure(failure.value, "codex_response_id_mismatch")


def test_duplicate_response_cannot_satisfy_a_later_request(rpc_factory):
    rpc = rpc_factory(
        """
        request = read()
        response = {"id": request["id"], "result": {"first": True}}
        send(response)
        send(response)
        request = read()
        send({"id": request["id"], "result": {"second": True}})
        """
    )
    with pytest.raises(EngineUnavailableError) as failure:
        rpc.call("initialize", {})
        rpc.call("account/read", {})
    assert_private_failure(failure.value, "codex_response_id_mismatch")


@pytest.mark.parametrize(
    ("wire", "reason"),
    [
        (b"fixture-codex-private-value-91374\n", "codex_invalid_json"),
        (b'{"id":1,"id":1,"result":{}}\n', "codex_invalid_json"),
        (b'{"id":1,"result":{"value":NaN}}\n', "codex_invalid_json"),
        (b'{"id":1,"result":{"value":"\xff"}}\n', "codex_invalid_json"),
        (b"[]\n", "codex_invalid_response"),
        (b'{"result":{}}\n', "codex_invalid_response"),
        (b'{"id":1,"result":{},"error":{}}\n', "codex_invalid_response"),
        (b'{"id":1,"result":null}\n', "codex_invalid_response"),
        (b'{"method":"event","params":[]}\n', "codex_invalid_notification"),
    ],
)
def test_invalid_protocol_is_rejected_without_untrusted_text(rpc_factory, wire, reason):
    rpc = rpc_factory(
        f"""
        read()
        sys.stdout.buffer.write({wire!r})
        sys.stdout.buffer.flush()
        sys.stdin.read()
        """
    )
    with pytest.raises(EngineUnavailableError) as failure:
        rpc.call("account/read", {})
    assert_private_failure(failure.value, reason)


def test_rpc_error_and_stderr_are_not_exposed(rpc_factory, capfd):
    rpc = rpc_factory(
        f"""
        request = read()
        print({SECRET!r}, file=sys.stderr, flush=True)
        send({{"id": request["id"], "error": {{
            "code": -32000, "message": {SECRET!r}, "data": {{"private": {SECRET!r}}}
        }}}})
        """
    )
    with pytest.raises(EngineUnavailableError) as failure:
        rpc.call("account/read", {})
    assert_private_failure(failure.value, "codex_rpc_failed")
    assert SECRET not in repr(capfd.readouterr())


@pytest.mark.parametrize("identifier", [17, "fixture-request"])
def test_unknown_server_request_receives_error_and_is_never_executed(
    rpc_factory, monkeypatch, identifier
):
    rpc = rpc_factory(
        f"""
        read()
        send({{"id": {identifier!r}, "method": "item/commandExecution/requestApproval",
               "params": {{"command": {SECRET!r}}}}})
        sys.stdin.read()
        """
    )
    descriptor, written = rpc_factory.processes[0].stdin.fileno(), bytearray()
    real_write = os.write

    def observe_write(fd, data):
        amount = real_write(fd, data)
        if fd == descriptor:
            written.extend(data[:amount])
        return amount

    monkeypatch.setattr(_rpc.os, "write", observe_write)
    with pytest.raises(EngineUnavailableError) as failure:
        rpc.call("turn/start", {})
    assert_private_failure(failure.value, "codex_server_request_rejected")
    messages = [json.loads(line) for line in written.splitlines()]
    assert len(messages) == 2
    assert messages[1]["id"] == identifier
    assert messages[1]["error"]["code"] == -32601
    assert "result" not in messages[1] and SECRET not in str(messages[1])


@pytest.mark.parametrize("newline", [False, True])
def test_message_limit_applies_before_or_after_newline(rpc_factory, monkeypatch, newline):
    monkeypatch.setattr(_rpc, "MAX_MESSAGE_BYTES", 128)
    payload = b"x" * 129 + (b"\n" if newline else b"")
    rpc = rpc_factory(
        f"""
        read()
        sys.stdout.buffer.write({payload!r})
        sys.stdout.buffer.flush()
        sys.stdin.read()
        """
    )
    with pytest.raises(EngineUnavailableError) as failure:
        rpc.call("account/read", {})
    assert_private_failure(failure.value, "codex_response_limit")


def test_session_limit_is_cumulative_across_successful_calls(rpc_factory, monkeypatch):
    monkeypatch.setattr(_rpc, "MAX_SESSION_BYTES", 140)
    rpc = rpc_factory(
        """
        while True:
            request = read()
            send({"id": request["id"], "result": {"padding": "x" * 50}})
        """
    )
    assert rpc.call("first", {}) == {"padding": "x" * 50}
    with pytest.raises(EngineUnavailableError) as failure:
        rpc.call("second", {})
    assert_private_failure(failure.value, "codex_session_limit")


def test_notification_queue_is_bounded_while_waiting_for_response(rpc_factory, monkeypatch):
    monkeypatch.setattr(_rpc, "MAX_NOTIFICATIONS", 2)
    rpc = rpc_factory(
        """
        request = read()
        for index in range(3):
            send({"method": "progress", "params": {"index": index}})
        send({"id": request["id"], "result": {}})
        """
    )
    with pytest.raises(EngineUnavailableError) as failure:
        rpc.call("turn/start", {})
    assert_private_failure(failure.value, "codex_notification_limit")


def test_request_validation_does_not_mark_unsent_payload_as_sent(rpc_factory, monkeypatch):
    monkeypatch.setattr(_rpc, "MAX_MESSAGE_BYTES", 128)
    rpc = rpc_factory("sys.stdin.read()")
    sent = []
    with pytest.raises(EngineUnavailableError) as failure:
        rpc.call("turn/start", {"text": SECRET * 100}, on_sent=lambda: sent.append(True))
    assert_private_failure(failure.value, "codex_request_limit")
    assert sent == []


def test_eof_is_reported_without_child_stderr(rpc_factory):
    rpc = rpc_factory(f"read()\nprint({SECRET!r}, file=sys.stderr)\nsys.exit(42)")
    with pytest.raises(EngineUnavailableError) as failure:
        rpc.call("account/read", {})
    assert_private_failure(failure.value, "codex_process_eof")


def test_absolute_deadline_is_not_extended_by_continuous_notifications(rpc_factory):
    rpc = rpc_factory(
        """
        request = read()
        send({"id": request["id"], "result": {}})
        read()
        for index in range(300):
            send({"method": "progress", "params": {"index": index}})
            time.sleep(0.01)
        """
    )
    rpc.call("initialize", {})
    started = time.monotonic()
    with pytest.raises(EngineUnavailableError) as failure:
        rpc.call("turn/start", {}, timeout=0.2)
    assert_private_failure(failure.value, "codex_rpc_timeout")
    assert time.monotonic() - started < 1.5


def test_absolute_deadline_also_applies_to_already_buffered_notifications(rpc_factory, monkeypatch):
    rpc = rpc_factory(
        """
        request = read()
        for method in ["progress", "progress", "done"]:
            send({"method": method, "params": {}})
        send({"id": request["id"], "result": {}})
        sys.stdin.read()
        """
    )
    rpc.call("turn/start", {})
    clock = [0.0]
    monkeypatch.setattr(_rpc, "time", SimpleNamespace(monotonic=lambda: clock[0]))

    def predicate(message):
        clock[0] += 0.6
        return message["method"] == "done"

    with pytest.raises(EngineUnavailableError) as failure:
        rpc.wait_for(predicate, timeout=1)
    assert_private_failure(failure.value, "codex_rpc_timeout")


def test_blocked_stdin_write_has_an_absolute_deadline(rpc_factory):
    rpc = rpc_factory("time.sleep(30)")
    sent = []
    started = time.monotonic()
    with pytest.raises(EngineUnavailableError) as failure:
        rpc.call(
            "turn/start",
            {"text": "x" * (2 * 1024 * 1024)},
            timeout=0.2,
            on_sent=lambda: sent.append(True),
        )
    assert_private_failure(failure.value, "codex_rpc_timeout")
    assert time.monotonic() - started < 1.5
    assert sent == [True]


@pytest.mark.parametrize("direction", ["send", "receive"])
@pytest.mark.parametrize("stop_method", ["close", "cancel"])
def test_close_and_cancel_are_linearized_with_in_flight_io(rpc_factory, direction, stop_method):
    rpc = rpc_factory(
        "request = read(); send({'id': request['id'], 'result': {'ok': True}}); sys.stdin.read()"
    )
    process = rpc._process
    descriptor = process.stdin.fileno() if direction == "send" else process.stdout.fileno()
    event = _rpc.selectors.EVENT_WRITE if direction == "send" else _rpc.selectors.EVENT_READ
    paused = threading.Event()
    resume = threading.Event()
    real_wait = rpc._wait

    def pause_after_readiness(fd, requested, deadline, lease=None):
        real_wait(fd, requested, deadline, lease)
        if fd == descriptor and requested == event and not paused.is_set():
            paused.set()
            assert resume.wait(3), "fixture I/O was not resumed"

    rpc._wait = pause_after_readiness
    with ThreadPoolExecutor(max_workers=2) as executor:
        call = executor.submit(rpc.call, "fixture/race", {}, timeout=5)
        assert paused.wait(3), f"fixture did not pause {direction}"
        stop = executor.submit(getattr(rpc, stop_method))
        replacement_read = replacement_write = None
        try:
            stopped = rpc._closed if stop_method == "close" else rpc._cancelled
            assert stopped.wait(1)
            stop.result(timeout=3)
            with pytest.raises(OSError):
                os.fstat(descriptor)
            pipe_read, pipe_write = os.pipe()
            if direction == "send":
                replacement_read = os.dup(pipe_read)
                os.close(pipe_read)
                os.dup2(pipe_write, descriptor)
                replacement_write = descriptor
                if pipe_write != descriptor:
                    os.close(pipe_write)
            else:
                os.dup2(pipe_read, descriptor)
                replacement_read = descriptor
                if pipe_read != descriptor:
                    os.close(pipe_read)
                replacement_write = pipe_write
                os.write(replacement_write, b"replacement-marker")
        finally:
            resume.set()
        expected = EngineUnavailableError if stop_method == "close" else CancelledError
        with pytest.raises(expected):
            call.result(timeout=3)
        assert replacement_read is not None and replacement_write is not None
        if direction == "send":
            os.set_blocking(replacement_read, False)
            with pytest.raises(BlockingIOError):
                os.read(replacement_read, 1)
        else:
            assert os.read(replacement_read, 64) == b"replacement-marker"
        os.close(replacement_read)
        os.close(replacement_write)

    continuing = rpc_factory(
        "request = read(); send({'id': request['id'], 'result': {'continued': True}}); "
        "sys.stdin.read()"
    )
    assert continuing.call("fixture/continue", {}) == {"continued": True}


@pytest.mark.parametrize("stop_method", ["close", "cancel"])
def test_large_nonblocking_write_does_not_delay_close_or_cancel(rpc_factory, stop_method):
    rpc = rpc_factory("time.sleep(30)")
    process = rpc._process
    assert not os.get_blocking(process.stdin.fileno())
    started = threading.Event()
    with ThreadPoolExecutor(max_workers=2) as executor:
        call = executor.submit(
            rpc.call,
            "turn/start",
            {"text": "x" * (2 * 1024 * 1024)},
            timeout=10,
            on_sent=started.set,
        )
        assert started.wait(3)
        before = time.monotonic()
        stop = executor.submit(getattr(rpc, stop_method))
        stop.result(timeout=3)
        assert time.monotonic() - before < 2
        expected = EngineUnavailableError if stop_method == "close" else CancelledError
        with pytest.raises(expected):
            call.result(timeout=3)


def test_close_does_not_wait_for_logical_rpc_lock_or_queued_call(rpc_factory, tmp_path):
    rpc = rpc_factory('read()\nPath("received").touch()\ntime.sleep(30)')
    with ThreadPoolExecutor(max_workers=2) as executor:
        active = executor.submit(rpc.call, "fixture/active", {}, timeout=10)
        wait_for_file(tmp_path / "received")
        queued = executor.submit(rpc.call, "fixture/queued", {}, timeout=10)
        started = time.monotonic()
        rpc.close()
        assert time.monotonic() - started < 2
        for future in (active, queued):
            with pytest.raises(EngineUnavailableError):
                future.result(timeout=3)


def test_queued_call_fails_closed_while_active_on_sent_callback_is_blocked(rpc_factory):
    rpc = rpc_factory("time.sleep(30)")
    callback_started = threading.Event()
    release_callback = threading.Event()

    def block_on_sent():
        callback_started.set()
        assert release_callback.wait(5)

    with ThreadPoolExecutor(max_workers=2) as executor:
        active = executor.submit(
            rpc.call,
            "fixture/active",
            {},
            timeout=10,
            on_sent=block_on_sent,
        )
        assert callback_started.wait(3)
        queued = executor.submit(rpc.call, "fixture/queued", {}, timeout=10)
        try:
            rpc.close()
            with pytest.raises(EngineUnavailableError) as failure:
                queued.result(timeout=2)
            assert_private_failure(failure.value, "codex_process_closed")
            assert not active.done()
        finally:
            release_callback.set()
        with pytest.raises(EngineUnavailableError):
            active.result(timeout=3)


@pytest.mark.parametrize("external", [False, True])
def test_cancellation_interrupts_pending_read_and_close_reaps_child(
    rpc_factory, tmp_path, external
):
    cancelled = threading.Event()
    rpc = rpc_factory(
        'read()\nPath("received").touch()\ntime.sleep(30)', should_cancel=cancelled.is_set
    )
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(rpc.call, "turn/start", {}, timeout=5)
        try:
            wait_for_file(tmp_path / "received")
            if external:
                rpc.cancel()
            else:
                cancelled.set()
            with pytest.raises(CancelledError):
                future.result(timeout=2)
        finally:
            rpc.close()
    assert rpc_factory.processes[0].poll() is not None
    if external:
        with pytest.raises(EngineUnavailableError) as failure:
            rpc.call("account/read", {})
        assert_private_failure(failure.value, "codex_process_closed")


def test_already_cancelled_operation_never_spawns_a_child(rpc_factory):
    with pytest.raises(CancelledError):
        rpc_factory("raise AssertionError('must not execute')", should_cancel=lambda: True)
    assert rpc_factory.processes == []


def test_cancel_between_readiness_and_eof_is_still_reported_as_cancelled(rpc_factory, monkeypatch):
    cancelled = threading.Event()
    rpc = rpc_factory(
        'read()\nprint("", flush=True)\nsys.stdin.read()',
        should_cancel=cancelled.is_set,
    )
    stdout_lease = rpc_factory.processes[0].stdout_lease
    real_read = _rpc._supervisor._lease_read
    observed = []

    def cancel_before_eof(lease, size):
        if lease is stdout_lease and not observed:
            # Make the cancellation arrive after select reported readable, but
            # before the first read observes that the child has been stopped.
            observed.append(real_read(lease, size))
            cancelled.set()
            return b""
        return real_read(lease, size)

    monkeypatch.setattr(_rpc._supervisor, "_lease_read", cancel_before_eof)
    with pytest.raises(CancelledError):
        rpc.call("turn/start", {})
    assert observed == [b"\n"]


@pytest.mark.parametrize("leader_exits", [False, True])
def test_close_kills_owned_descendants_even_if_group_leader_has_exited(
    rpc_factory, monkeypatch, leader_exits
):
    child = (
        "import signal,time; from pathlib import Path; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "Path('descendant-ready').touch(); time.sleep(30)"
    )
    rpc = rpc_factory(
        f"""
        import subprocess
        request = read()
        child = subprocess.Popen([sys.executable, "-I", "-c", {child!r}],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
        while not Path("descendant-ready").exists():
            time.sleep(0.01)
        send({{"id": request["id"], "result": {{"pgid": os.getpgid(child.pid)}}}})
        if {leader_exits!r}:
            sys.exit(0)
        sys.stdin.read()
        """
    )
    process = rpc_factory.processes[0]
    assert rpc.call("fixture/spawn", {}) == {"pgid": process.pid}
    if leader_exits:
        # Observe leader exit without reaping its PID before group cleanup.
        assert select.select([process.stdout], [], [], 2)[0]
        assert os.read(process.stdout.fileno(), 1) == b""
    rpc.close()
    assert process.poll() is not None
    with pytest.raises(ProcessLookupError):
        os.killpg(process.pid, 0)
    rpc.close()


@pytest.mark.skipif(os.name != "posix", reason="POSIX parent-death lifeline")
def test_server_sigkill_watchdog_kills_eof_exiting_leader_and_term_ignoring_descendant(
    tmp_path,
):
    identity = tmp_path / "parent-death.ids"
    descendant_ready = tmp_path / "descendant.ready"
    child = (
        "import signal,time; from pathlib import Path; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        f"Path({str(descendant_ready)!r}).touch(); time.sleep(30)"
    )
    worker = PREAMBLE + textwrap.dedent(
        f"""
        import subprocess
        request = read()
        descendant = subprocess.Popen(
            [sys.executable, "-I", "-c", {child!r}],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        while not Path({str(descendant_ready)!r}).exists():
            time.sleep(0.01)
        send({{"id": request["id"], "result": {{"pid": descendant.pid}}}})
        sys.stdin.read()
        """
    )
    parent_program = textwrap.dedent(
        f"""
        import os, sys, time
        from pathlib import Path
        from narumi.providers.codex._rpc import StdioRPC
        rpc = StdioRPC(
            [sys.executable, "-I", "-u", "-c", {worker!r}],
            env={{}}, cwd=Path({str(tmp_path)!r}), timeout=3,
        )
        result = rpc.call("fixture/spawn", {{}})
        process = rpc._process
        unrelated = os.fork()
        if unrelated == 0:
            time.sleep(30)
            os._exit(0)
        Path({str(identity)!r}).write_text(
            f"{{process.pid}} {{process.watchdog.pid}} {{result['pid']}} {{unrelated}}"
        )
        time.sleep(30)
        """
    )
    parent = subprocess.Popen(
        [sys.executable, "-I", "-c", parent_program],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        cwd=tmp_path,
        env={"PYTHONPATH": str(tmp_path)},
        start_new_session=True,
    )
    group = unrelated = None
    try:
        wait_for_file(identity)
        group, watchdog, descendant, unrelated = (
            int(value) for value in identity.read_text().split()
        )
        assert group not in {parent.pid, watchdog, descendant}
        os.kill(parent.pid, signal.SIGKILL)
        parent.wait(timeout=3)
        wait_for_group_quiescent(group)
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=3)
        if group is not None:
            try:
                os.killpg(group, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        if unrelated is not None:
            try:
                os.kill(unrelated, signal.SIGKILL)
            except ProcessLookupError:
                pass


@pytest.mark.skipif(os.name != "posix", reason="POSIX watchdog recovery")
def test_live_rpc_recovers_group_after_watchdog_sigkill(rpc_factory, tmp_path):
    child = (
        "import signal,time; from pathlib import Path; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "Path('watchdog-descendant.ready').touch(); time.sleep(30)"
    )
    rpc = rpc_factory(
        f"""
        import subprocess
        request = read()
        descendant = subprocess.Popen(
            [sys.executable, "-I", "-c", {child!r}],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        while not Path("watchdog-descendant.ready").exists():
            time.sleep(0.01)
        send({{"id": request["id"], "result": {{"pid": descendant.pid}}}})
        time.sleep(30)
        """
    )
    process = rpc_factory.processes[0]
    rpc.call("fixture/spawn", {})
    os.kill(process.watchdog.pid, signal.SIGKILL)
    process.watchdog.wait(timeout=3)
    with pytest.raises(EngineUnavailableError) as failure:
        rpc.call("account/read", {})
    assert_private_failure(failure.value, "codex_process_unavailable")
    wait_for_group_quiescent(process.pid)


@pytest.mark.skipif(os.name != "posix", reason="POSIX fork descriptor cleanup")
@pytest.mark.filterwarnings("ignore:This process .* is multi-threaded.*:DeprecationWarning")
def test_fork_child_closes_all_rpc_descriptors_while_parent_rpc_continues(rpc_factory):
    first = rpc_factory("sys.stdin.read()")
    second = rpc_factory(
        "request = read(); send({'id': request['id'], 'result': {'continued': True}}); "
        "sys.stdin.read()"
    )
    descriptors = {
        first._process.stdin.fileno(),
        first._process.stdout.fileno(),
        first._process.lifeline_write,
        first._process.guardian_write,
        second._process.stdin.fileno(),
        second._process.stdout.fileno(),
        second._process.lifeline_write,
        second._process.guardian_write,
    }
    inherited_stdin = second._process.stdin
    inherited_stdout = second._process.stdout
    assert descriptors.issubset(_rpc._supervisor._child_close_descriptors)
    report_read, report_write = os.pipe()
    ready = threading.Barrier(2)

    def close_first():
        ready.wait()
        first.close()

    thread = threading.Thread(target=close_first)
    thread.start()
    ready.wait()
    child = os.fork()
    if child == 0:
        os.close(report_read)
        open_descriptors = []
        readable_descriptors = []
        writable_descriptors = []
        for descriptor in descriptors:
            try:
                os.fstat(descriptor)
            except OSError:
                pass
            else:
                open_descriptors.append(descriptor)
            try:
                os.read(descriptor, 1)
            except OSError:
                pass
            else:
                readable_descriptors.append(descriptor)
            try:
                os.write(descriptor, b"x")
            except OSError:
                pass
            else:
                writable_descriptors.append(descriptor)
        replacement_read, replacement_write = os.pipe()
        replacement_descriptors = {replacement_read, replacement_write}
        replacement_reused = bool(replacement_descriptors.intersection(descriptors))
        second.close()
        inherited_stdin.close()
        del inherited_stdout
        gc.collect()
        replacement_survived = all(
            _descriptor_available(descriptor) for descriptor in replacement_descriptors
        )
        payload = json.dumps(
            {
                "registry": sorted(_rpc._supervisor._child_close_descriptors),
                "open": open_descriptors,
                "readable": readable_descriptors,
                "writable": writable_descriptors,
                "replacement_reused": replacement_reused,
                "replacement_survived": replacement_survived,
            }
        ).encode()
        os.write(report_write, payload)
        os.close(report_write)
        os._exit(0)
    os.close(report_write)
    payload = json.loads(os.read(report_read, 4096))
    os.close(report_read)
    os.waitpid(child, 0)
    thread.join(timeout=6)
    assert not thread.is_alive()
    assert payload == {
        "registry": [],
        "open": [],
        "readable": [],
        "writable": [],
        "replacement_reused": True,
        "replacement_survived": True,
    }
    assert second.call("fixture/continue", {}) == {"continued": True}
    second.close()
    assert descriptors.isdisjoint(_rpc._supervisor._child_close_descriptors)


@pytest.mark.skipif(os.name != "posix", reason="POSIX fork lock reset")
@pytest.mark.filterwarnings("ignore:This process .* is multi-threaded.*:DeprecationWarning")
def test_fork_child_replaces_inherited_rpc_locks_and_fails_closed(rpc_factory):
    rpc = rpc_factory("sys.stdin.read()")
    report_read, report_write = os.pipe()
    callback_lock = threading.Lock()
    callback_lock.acquire()

    def inherited_callback():
        with callback_lock:
            return False

    rpc._should_cancel = inherited_callback
    rpc._io_gate.acquire()
    rpc._lifecycle_lock.acquire()
    rpc._rpc_lock.acquire()
    try:
        child = os.fork()
        if child == 0:
            os.close(report_read)
            try:
                rpc.close()
                try:
                    rpc.call("fixture/forbidden", {}, timeout=0.1)
                except EngineUnavailableError as error:
                    payload = error.details
                else:
                    payload = {"reason": "unexpected_success"}
                os.write(report_write, json.dumps(payload).encode())
            finally:
                os.close(report_write)
                os._exit(0)
    finally:
        rpc._rpc_lock.release()
        rpc._lifecycle_lock.release()
        rpc._io_gate.release()
        callback_lock.release()
    os.close(report_write)
    assert json.loads(os.read(report_read, 1024)) == {"reason": "codex_process_closed"}
    os.close(report_read)
    os.waitpid(child, 0)


@pytest.mark.skipif(os.name != "posix", reason="POSIX group-anchor recovery")
def test_anchor_recovers_descendant_after_watchdog_and_leader_die(rpc_factory):
    child = "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)"
    rpc = rpc_factory(
        f"""
        import signal, subprocess
        request = read()
        descendant = subprocess.Popen(
            [sys.executable, "-I", "-c", {child!r}],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        send({{"id": request["id"], "result": {{"pid": descendant.pid}}}})
        os.kill(os.getppid(), signal.SIGKILL)
        sys.exit(0)
        """
    )
    process = rpc_factory.processes[0]
    rpc.call("fixture/spawn", {})
    process.watchdog.wait(timeout=3)
    deadline = time.monotonic() + 3
    while process.identity in (_rpc._supervisor._group_identities(process.pid) or set()):
        assert time.monotonic() < deadline
        threading.Event().wait(0.01)
    assert process.anchor_identity in (_rpc._supervisor._group_identities(process.pid) or set())
    rpc.close()
    wait_for_group_quiescent(process.pid)


def test_unwatched_cleanup_refuses_reused_process_group(monkeypatch):
    original = (1234, "original-start")
    monkeypatch.setattr(
        _rpc._supervisor,
        "_group_identities",
        lambda _group: {(1234, "reused-start"), (5678, "unrelated")},
    )
    signalled = []
    monkeypatch.setattr(
        _rpc._supervisor.os,
        "killpg",
        lambda group, requested: signalled.append((group, requested)),
    )
    assert _rpc._supervisor._terminate_unwatched(1234, (original,)) is False
    assert signalled == []


def test_start_failure_does_not_expose_command_or_environment(monkeypatch, tmp_path):
    def fail_start(*args, **kwargs):
        raise OSError(SECRET)

    monkeypatch.setattr(_rpc._supervisor, "start", fail_start)
    with pytest.raises(EngineUnavailableError) as failure:
        StdioRPC([SECRET], env={"PRIVATE": SECRET}, cwd=tmp_path)
    assert_private_failure(failure.value, "codex_process_unavailable")


def test_watchdog_start_failure_does_not_leave_registered_descriptors(monkeypatch, tmp_path):
    baseline = set(_rpc._supervisor._child_close_descriptors)

    def fail_start(*_args, **_kwargs):
        raise OSError(SECRET)

    monkeypatch.setattr(_rpc._supervisor.subprocess, "Popen", fail_start)
    with pytest.raises(EngineUnavailableError) as failure:
        StdioRPC([sys.executable, "-I", "-c", "pass"], env={}, cwd=tmp_path)
    assert_private_failure(failure.value, "codex_process_unavailable")
    assert set(_rpc._supervisor._child_close_descriptors) == baseline
    assert list(tmp_path.iterdir()) == []


def test_supervised_process_constructor_failure_releases_every_lease(monkeypatch, tmp_path):
    baseline = set(_rpc._supervisor._child_close_descriptors)
    real_process = _rpc._supervisor.SupervisedProcess

    class FailingProcess(real_process):
        def __init__(self, *_args, **_kwargs):
            raise OSError("fixture constructor failure")

    monkeypatch.setattr(_rpc._supervisor, "SupervisedProcess", FailingProcess)
    with pytest.raises(EngineUnavailableError) as failure:
        StdioRPC(
            [sys.executable, "-I", "-u", "-c", PREAMBLE + "sys.stdin.read()"],
            env={},
            cwd=tmp_path,
        )
    assert_private_failure(failure.value, "codex_process_unavailable")
    assert set(_rpc._supervisor._child_close_descriptors) == baseline
    assert list(tmp_path.iterdir()) == []


def test_parent_failure_before_watchdog_status_cleans_launcher_and_descriptors(
    monkeypatch, tmp_path
):
    baseline = set(_rpc._supervisor._child_close_descriptors)
    watchdogs = []
    real_popen = _rpc._supervisor.subprocess.Popen

    def observe(command, **kwargs):
        process = real_popen(command, **kwargs)
        if command[:4] == (sys.executable, "-I", "-S", "-c"):
            watchdogs.append(process)
        return process

    def fail_reader(_path, _owner=None):
        threading.Event().wait(0.1)
        raise OSError("fixture failure before status")

    monkeypatch.setattr(_rpc._supervisor.subprocess, "Popen", observe)
    monkeypatch.setattr(_rpc._supervisor, "_open_registered_fifo_reader", fail_reader)
    with pytest.raises(EngineUnavailableError) as failure:
        StdioRPC(
            [sys.executable, "-I", "-u", "-c", PREAMBLE + "sys.stdin.read()"],
            env={},
            cwd=tmp_path,
        )
    assert_private_failure(failure.value, "codex_process_unavailable")
    assert len(watchdogs) == 1
    watchdogs[0].wait(timeout=6)
    assert set(_rpc._supervisor._child_close_descriptors) == baseline
    assert list(tmp_path.iterdir()) == []


def test_failed_fifo_creation_removes_only_paths_created_by_this_start(monkeypatch, tmp_path):
    token = "fixed-token"
    request = tmp_path / f".codex-request-{token}.fifo"
    response = tmp_path / f".codex-response-{token}.fifo"
    guardian = tmp_path / f".codex-guardian-{token}.fifo"
    response.write_bytes(b"existing-response")
    guardian.write_bytes(b"existing-guardian")
    monkeypatch.setattr(_rpc._supervisor.secrets, "token_hex", lambda _size: token)
    with pytest.raises(FileExistsError):
        _rpc._supervisor.start([sys.executable, "-I", "-c", "pass"], {}, tmp_path)
    assert not request.exists()
    assert response.read_bytes() == b"existing-response"
    assert guardian.read_bytes() == b"existing-guardian"


def test_unlink_failure_after_success_does_not_abandon_owned_process(rpc_factory, monkeypatch):
    real_unlink = Path.unlink
    failed = []
    visited = []

    def fail_once(path, *args, **kwargs):
        if path.name.startswith(".codex-"):
            visited.append(path)
            if not failed:
                failed.append(path)
                raise PermissionError("fixture unlink failure")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_once)
    rpc = rpc_factory(
        "request = read(); send({'id': request['id'], 'result': {'ok': True}}); sys.stdin.read()"
    )
    assert rpc.call("fixture/check", {}) == {"ok": True}
    assert len(visited) == 3
    real_unlink(failed[0])


@pytest.mark.parametrize("cleanup_error", [KeyboardInterrupt(), SystemExit(7)])
def test_path_cleanup_base_exception_cannot_steal_completed_process_ownership(
    rpc_factory, monkeypatch, cleanup_error
):
    baseline = set(_rpc._supervisor._child_close_descriptors)
    real_unlink = Path.unlink
    failed = []
    visited = []

    def interrupt_once(path, *args, **kwargs):
        if path.name.startswith(".codex-"):
            visited.append(path)
            if not failed:
                failed.append(path)
                raise cleanup_error
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", interrupt_once)
    rpc = rpc_factory(
        "request = read(); send({'id': request['id'], 'result': {'owned': True}}); sys.stdin.read()"
    )
    assert rpc.call("fixture/owned", {}) == {"owned": True}
    assert len(visited) == 3
    rpc.close()
    assert set(_rpc._supervisor._child_close_descriptors) == baseline
    real_unlink(failed[0])


def test_constructor_base_exception_after_process_start_still_reaps_group(monkeypatch, tmp_path):
    baseline = set(_rpc._supervisor._child_close_descriptors)
    process = []
    real_start = _rpc._supervisor.start
    real_set_blocking = os.set_blocking
    calls = 0

    def observe_start(*args, **kwargs):
        started = real_start(*args, **kwargs)
        process.append(started)
        return started

    def interrupt_second(descriptor, blocking):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise KeyboardInterrupt
        real_set_blocking(descriptor, blocking)

    monkeypatch.setattr(_rpc._supervisor, "start", observe_start)
    monkeypatch.setattr(_rpc.os, "set_blocking", interrupt_second)
    with pytest.raises(KeyboardInterrupt):
        StdioRPC(
            [sys.executable, "-I", "-u", "-c", PREAMBLE + "sys.stdin.read()"],
            env={},
            cwd=tmp_path,
        )
    assert len(process) == 1
    process[0].wait(timeout=6)
    wait_for_group_quiescent(process[0].pid)
    assert set(_rpc._supervisor._child_close_descriptors) == baseline


def test_registered_pipe_is_failure_atomic_when_second_registration_raises(monkeypatch):
    baseline_registry = dict(_rpc._supervisor._child_close_descriptors)
    baseline_descriptors = set(os.listdir("/dev/fd"))
    real_register = _rpc._supervisor._register_descriptor
    calls = 0

    def fail_second(descriptor):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise MemoryError("fixture registration failure")
        return real_register(descriptor)

    monkeypatch.setattr(_rpc._supervisor, "_register_descriptor", fail_second)
    with pytest.raises(MemoryError):
        _rpc._supervisor._registered_pipe()
    assert _rpc._supervisor._child_close_descriptors == baseline_registry
    assert set(os.listdir("/dev/fd")) == baseline_descriptors


def test_initial_start_cleanup_retries_a_transient_base_exception(monkeypatch, tmp_path):
    baseline_registry = dict(_rpc._supervisor._child_close_descriptors)
    baseline_descriptors = set(os.listdir("/dev/fd"))
    real_pipe = _rpc._supervisor._registered_pipe
    real_release = _rpc._supervisor._release_descriptor
    pipe_calls = 0
    interrupted = False

    def fail_second_pipe(owner=None):
        nonlocal pipe_calls
        pipe_calls += 1
        if pipe_calls == 2:
            raise OSError("fixture initial setup failure")
        return real_pipe(owner)

    def interrupt_first_release(lease):
        nonlocal interrupted
        if not interrupted:
            interrupted = True
            raise KeyboardInterrupt
        return real_release(lease)

    monkeypatch.setattr(_rpc._supervisor, "_registered_pipe", fail_second_pipe)
    monkeypatch.setattr(_rpc._supervisor, "_release_descriptor", interrupt_first_release)
    with pytest.raises(OSError, match="initial setup failure"):
        _rpc._supervisor.start([sys.executable, "-I", "-c", "pass"], {}, tmp_path)
    assert _rpc._supervisor._child_close_descriptors == baseline_registry
    assert set(os.listdir("/dev/fd")) == baseline_descriptors
    assert list(tmp_path.iterdir()) == []


def test_post_launch_cleanup_retries_transient_base_exception_and_reaps_group(
    monkeypatch, tmp_path
):
    baseline_registry = dict(_rpc._supervisor._child_close_descriptors)
    baseline_descriptors = set(os.listdir("/dev/fd"))
    real_identity = _rpc._supervisor._process_identity
    real_release = _rpc._supervisor._release_descriptor
    identity_calls = 0
    cleanup_started = False
    interrupted = False
    group = []
    real_status = _rpc._supervisor._read_status

    def observe_status(*args, **kwargs):
        status = real_status(*args, **kwargs)
        group.append(status[0])
        return status

    def fail_anchor_identity(*args, **kwargs):
        nonlocal identity_calls, cleanup_started
        identity_calls += 1
        identity = real_identity(*args, **kwargs)
        if identity_calls == 2:
            cleanup_started = True
            return None
        return identity

    def interrupt_cleanup_once(lease):
        nonlocal interrupted
        if cleanup_started and not interrupted:
            interrupted = True
            raise KeyboardInterrupt
        return real_release(lease)

    monkeypatch.setattr(_rpc._supervisor, "_read_status", observe_status)
    monkeypatch.setattr(_rpc._supervisor, "_process_identity", fail_anchor_identity)
    monkeypatch.setattr(_rpc._supervisor, "_release_descriptor", interrupt_cleanup_once)
    with pytest.raises(EngineUnavailableError) as failure:
        StdioRPC(
            [sys.executable, "-I", "-u", "-c", PREAMBLE + "sys.stdin.read()"],
            env={},
            cwd=tmp_path,
        )
    assert_private_failure(failure.value, "codex_process_unavailable")
    assert len(group) == 1
    wait_for_group_quiescent(group[0])
    assert _rpc._supervisor._child_close_descriptors == baseline_registry
    assert set(os.listdir("/dev/fd")) == baseline_descriptors
    assert list(tmp_path.iterdir()) == []


def test_termination_base_exception_still_closes_lifeline_and_is_retryable(
    rpc_factory, monkeypatch
):
    rpc = rpc_factory("sys.stdin.read()")
    process = rpc._process
    real_release = _rpc._supervisor._release_stream
    interrupted = False

    def interrupt_once(stream, lease):
        nonlocal interrupted
        if not interrupted:
            interrupted = True
            raise KeyboardInterrupt
        return real_release(stream, lease)

    monkeypatch.setattr(_rpc._supervisor, "_release_stream", interrupt_once)
    with pytest.raises(KeyboardInterrupt):
        rpc.close()
    process.wait(timeout=6)
    wait_for_group_quiescent(process.pid)
    rpc.close()
    for lease in (
        process.stdin_lease,
        process.stdout_lease,
        process.lifeline_lease,
        process.guardian_lease,
    ):
        assert lease.descriptor not in _rpc._supervisor._child_close_descriptors


def test_failed_start_never_releases_a_reused_descriptor_from_parallel_rpc(
    monkeypatch, tmp_path, rpc_factory
):
    parallel_rpc = rpc_factory(
        "request = read(); send({'id': request['id'], 'result': {'continued': True}}); "
        "sys.stdin.read()"
    )
    real_identity = _rpc._supervisor._process_identity
    real_release = _rpc._supervisor._release_descriptor
    identity_failed = False
    replacement: dict[str, object] = {}

    def fail_first_identity(pid, *, expected_group):
        nonlocal identity_failed
        if not identity_failed:
            identity_failed = True
            return None
        return real_identity(pid, expected_group=expected_group)

    def observe_release(lease):
        descriptor = lease.descriptor
        try:
            is_fifo = stat.S_ISFIFO(os.fstat(descriptor).st_mode)
        except OSError:
            is_fifo = False
        released = real_release(lease)
        if not is_fifo or replacement:
            return released
        replacement_read, replacement_write = os.pipe()
        assert replacement_read == descriptor
        with _rpc._supervisor._fork_lock:
            replacement_lease = _rpc._supervisor._register_descriptor(replacement_read)
        replacement.update(
            read=replacement_read,
            write=replacement_write,
            lease=replacement_lease,
            stale=lease,
        )
        return released

    monkeypatch.setattr(_rpc._supervisor, "_process_identity", fail_first_identity)
    monkeypatch.setattr(_rpc._supervisor, "_release_descriptor", observe_release)
    try:
        with pytest.raises(EngineUnavailableError) as failure:
            StdioRPC(
                [sys.executable, "-I", "-u", "-c", PREAMBLE + "sys.stdin.read()"],
                env={},
                cwd=tmp_path,
            )
        assert_private_failure(failure.value, "codex_process_unavailable")
        replacement_read = replacement["read"]
        os.fstat(replacement_read)
        assert real_release(replacement["stale"]) is False
        os.fstat(replacement_read)
        assert parallel_rpc.call("fixture/continue", {}) == {"continued": True}
    finally:
        if replacement:
            real_release(replacement["lease"])
            os.close(replacement["write"])


def test_repeated_old_terminate_and_stream_release_cannot_close_reused_rpc_fd(rpc_factory):
    old_rpc = rpc_factory("sys.stdin.read()")
    old_process = old_rpc._process
    old_descriptors = {
        old_process.stdin_lease.descriptor,
        old_process.stdout_lease.descriptor,
        old_process.lifeline_lease.descriptor,
        old_process.guardian_lease.descriptor,
    }
    old_rpc.close()
    current_rpc = rpc_factory(
        "request = read(); send({'id': request['id'], 'result': {'continued': True}}); "
        "sys.stdin.read()"
    )
    current_process = current_rpc._process
    current_descriptors = {
        current_process.stdin_lease.descriptor,
        current_process.stdout_lease.descriptor,
        current_process.lifeline_lease.descriptor,
        current_process.guardian_lease.descriptor,
    }
    assert old_descriptors.intersection(current_descriptors)
    _rpc._supervisor.terminate(old_process)
    assert _rpc._supervisor._release_stream(old_process.stdin, old_process.stdin_lease) is False
    assert current_rpc.call("fixture/continue", {}) == {"continued": True}


def test_stale_lease_cannot_unregister_or_close_new_generation_of_same_fd():
    read_descriptor, write_descriptor = os.pipe()
    with _rpc._supervisor._fork_lock:
        stale = _rpc._supervisor._register_descriptor(read_descriptor)
    assert _rpc._supervisor._release_descriptor(stale) is True
    replacement_read, replacement_write = os.pipe()
    assert replacement_read == stale.descriptor
    with _rpc._supervisor._fork_lock:
        current = _rpc._supervisor._register_descriptor(replacement_read)
    try:
        assert current.token is not stale.token
        assert _rpc._supervisor._release_descriptor(stale) is False
        assert _rpc._supervisor._child_close_descriptors[replacement_read] is current.token
        os.fstat(replacement_read)
    finally:
        _rpc._supervisor._release_descriptor(current)
        os.close(write_descriptor)
        os.close(replacement_write)
