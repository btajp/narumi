"""Exercise private Codex pipes using only controlled local Python children."""

from __future__ import annotations

import json
import os
import select
import signal
import subprocess
import sys
import textwrap
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
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
def rpc_factory(tmp_path, monkeypatch):
    processes, launches, transports = [], [], []
    real_popen, real_killpg = subprocess.Popen, os.killpg

    def spawn(command, **kwargs):
        assert command[:4] == [sys.executable, "-I", "-u", "-c"]
        assert kwargs.get("shell", False) is False
        assert kwargs["start_new_session"] is True
        process = real_popen(command, **kwargs)
        processes.append(process)
        launches.append(kwargs)
        return process

    monkeypatch.setattr(_rpc.subprocess, "Popen", spawn)

    def create(script, *, env=None, **kwargs):
        command = [sys.executable, "-I", "-u", "-c", PREAMBLE + textwrap.dedent(script)]
        rpc = StdioRPC(command, env=env or {}, cwd=tmp_path, timeout=3, **kwargs)
        transports.append(rpc)
        return rpc

    create.processes, create.launches = processes, launches
    yield create
    try:
        for rpc in transports:
            rpc.close()
    finally:
        # A failing lifecycle assertion must not leave a fixture descendant alive.
        # Every process above was launched in its own fresh, test-owned session.
        for process in processes:
            if process.returncode is None:
                try:
                    real_killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                except PermissionError:
                    # Darwin can report EPERM for a group with only a zombie.
                    if process.poll() is None:
                        process.kill()
            process.wait(timeout=3)
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
                "explicit": os.environ.get("NARUMI_RPC_EXPLICIT"),
                "inherited": "NARUMI_RPC_PARENT_SECRET" in os.environ,
            }})
        """,
        env={"NARUMI_RPC_EXPLICIT": "fixture value"},
    )
    process, launch = rpc_factory.processes[0], rpc_factory.launches[0]
    assert launch["stderr"] == subprocess.DEVNULL
    assert launch["env"] == {"NARUMI_RPC_EXPLICIT": "fixture value"}
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
        with pytest.raises(CancelledError):
            rpc.call("account/read", {})


def test_already_cancelled_operation_never_spawns_a_child(rpc_factory):
    with pytest.raises(CancelledError):
        rpc_factory("raise AssertionError('must not execute')", should_cancel=lambda: True)
    assert rpc_factory.processes == []


def test_cancel_between_readiness_and_eof_is_still_reported_as_cancelled(rpc_factory, monkeypatch):
    rpc = rpc_factory('read()\nprint("", flush=True)\nsys.stdin.read()')
    descriptor, real_read = rpc_factory.processes[0].stdout.fileno(), os.read
    observed = []

    def cancel_before_eof(fd, size):
        if fd == descriptor and not observed:
            # Make the cancellation arrive after select reported readable, but
            # before the first read observes that the child has been stopped.
            observed.append(real_read(fd, size))
            rpc.cancel()
            eof = real_read(fd, size)
            assert eof == b""
            return eof
        return real_read(fd, size)

    monkeypatch.setattr(_rpc.os, "read", cancel_before_eof)
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
    signals, real_killpg = [], os.killpg

    def observe_signal(group, number):
        signals.append((group, number))
        return real_killpg(group, number)

    monkeypatch.setattr(_rpc.os, "killpg", observe_signal)
    rpc.close()
    assert process.poll() is not None
    assert (process.pid, signal.SIGKILL) in signals
    assert all(group == process.pid for group, _ in signals)
    rpc.close()


def test_start_failure_does_not_expose_command_or_environment(monkeypatch, tmp_path):
    def fail_start(*args, **kwargs):
        raise OSError(SECRET)

    monkeypatch.setattr(_rpc.subprocess, "Popen", fail_start)
    with pytest.raises(EngineUnavailableError) as failure:
        StdioRPC([SECRET], env={"PRIVATE": SECRET}, cwd=tmp_path)
    assert_private_failure(failure.value, "codex_process_unavailable")
