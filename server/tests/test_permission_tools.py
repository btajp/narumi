"""Permission MCP handlers: replay, busy gates, fresh diagnostics and shutdown."""

from __future__ import annotations

import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from conftest import PerCallClient, call
from narumi_server import recording
from narumi_server.app import dispatch
from narumi_server.context import ServerContext, build_context
from test_permission_setup import DENIED, GRANTED, wait_marker

TOOL = "configure_recording_permission"


def arguments(**extra) -> dict:
    return {"permission": "microphone", "action": "request", "request_id": str(uuid4()), **extra}


async def test_permission_mcp_roundtrip_without_recording(
    client: PerCallClient, ctx: ServerContext
):
    result = await call(client, TOOL, arguments())
    assert result == {
        "permission": "microphone",
        "action": "request",
        "permissions": GRANTED,
        "settings_opened": False,
    }
    assert await call(client, "get_recording_status") == {"active": False}
    assert await call(client, "list_meetings") == {"meetings": []}
    assert ctx.catalog.list_jobs() == []
    assert not list(ctx.meetings_root.glob("*"))
    info = await call(client, "get_server_info", {"refresh_permissions": True})
    assert info["capabilities"]["permission_setup_in_progress"] is False


async def test_server_instance_id_stays_stable_across_mcp_and_fresh_reads(
    client: PerCallClient, ctx: ServerContext
):
    first = await call(client, "get_server_info")
    refreshed = await call(client, "get_server_info", {"refresh_permissions": True})
    repeated = await call(client, "get_server_info")
    instance_id = first["server_instance_id"]
    assert instance_id == refreshed["server_instance_id"] == repeated["server_instance_id"]
    assert instance_id == ctx.server_instance_id
    assert str(UUID(instance_id)) == instance_id and UUID(instance_id).version == 4


def test_restarted_context_uses_new_instance_id_without_persisting_it(home: Path):
    first = build_context(home, validate_output=True)
    try:
        previous = dispatch(first, "get_server_info", {})
        assert not previous.is_error
        previous_id = previous.payload["server_instance_id"]
    finally:
        first.close()
    restarted = build_context(home, validate_output=True)
    try:
        fresh = dispatch(restarted, "get_server_info", {"refresh_permissions": True})
        assert not fresh.is_error
        assert fresh.payload["server_instance_id"] == restarted.server_instance_id
        assert restarted.server_instance_id != previous_id
        assert UUID(restarted.server_instance_id).version == 4
    finally:
        restarted.close()


def test_denial_is_replayed_without_second_prompt(ctx: ServerContext, monkeypatch, tmp_path: Path):
    marker = tmp_path / "permission.pid"
    monkeypatch.setenv("FAKE_RECORDER_PERMISSION_MARKER", str(marker))
    monkeypatch.setenv("FAKE_RECORDER_CHECK", json.dumps(DENIED))
    args = arguments()
    first = dispatch(ctx, TOOL, args)
    assert not first.is_error and first.payload["permissions"] == DENIED
    monkeypatch.setenv("FAKE_RECORDER_CHECK", json.dumps(GRANTED))
    replay = dispatch(ctx, TOOL, args)
    assert replay == first
    assert len(marker.read_text().splitlines()) == 1
    assert (
        dispatch(ctx, "get_server_info", {"refresh_permissions": True}).payload["capabilities"][
            "permissions"
        ]
        == GRANTED
    )
    changed = dispatch(ctx, TOOL, {**args, "permission": "screen_recording"})
    assert changed.is_error and changed.payload["error"]["code"] == "invalid_argument"
    other = dispatch(ctx, "rebuild_catalog", {"request_id": args["request_id"]})
    assert other.is_error and other.payload["error"]["code"] == "invalid_argument"


@pytest.mark.parametrize("first_fails", [False, True])
def test_same_id_and_competing_start_fail_immediately(
    ctx: ServerContext, tmp_path: Path, monkeypatch, first_fails: bool
):
    marker = tmp_path / "permission.pid"
    monkeypatch.setenv("FAKE_RECORDER_PERMISSION_MARKER", str(marker))
    monkeypatch.setenv("FAKE_RECORDER_PERMISSION_DELAY", "0.8")
    if first_fails:
        monkeypatch.setenv("FAKE_RECORDER_PERMISSION_EXIT", "3")
    args = arguments()
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(dispatch, ctx, TOOL, args)
        wait_marker(marker)
        replay = dispatch(ctx, TOOL, args)
        assert replay.is_error and replay.payload["error"]["code"] == "busy"
        assert not pending.done(), "the replay must not wait for the first result"
        competing = dispatch(ctx, TOOL, arguments(permission="screen_recording"))
        assert competing.is_error and competing.payload["error"]["code"] == "busy"
        start = dispatch(ctx, "start_recording", {"request_id": str(uuid4())})
        assert start.is_error and start.payload["error"]["code"] == "busy"
        assert not list(ctx.meetings_root.glob("*"))
        info = dispatch(ctx, "get_server_info", {"refresh_permissions": True})
        assert info.payload["capabilities"]["permission_setup_in_progress"] is True
        first = pending.result(timeout=5)
    assert first.is_error is first_fails
    assert len(marker.read_text().splitlines()) == 1  # no delayed second invocation after failure
    assert (ctx.catalog.get_request(args["request_id"]) is None) is first_fails
    assert not ctx.recorder.permission_setup_in_progress


def test_start_preflight_reserves_gate_before_bundle_creation(ctx: ServerContext, monkeypatch):
    entered, release = threading.Event(), threading.Event()

    def slow_permissions(*, max_age: float = 5):
        entered.set()
        assert release.wait(5)
        return GRANTED

    monkeypatch.setattr(ctx.recorder, "permissions", slow_permissions)
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(dispatch, ctx, "start_recording", {"request_id": str(uuid4())})
        assert entered.wait(5)
        try:
            action = dispatch(ctx, TOOL, arguments())
            assert action.is_error and action.payload["error"]["code"] == "busy"
            assert not list(ctx.meetings_root.glob("*"))
        finally:
            release.set()
        assert not pending.result(timeout=5).is_error
    stopped = dispatch(ctx, "stop_recording", {"request_id": str(uuid4()), "auto_process": False})
    assert not stopped.is_error


@pytest.mark.parametrize("raw", ["not-json", '{"permission":"microphone"}'])
def test_malformed_helper_result_is_never_cached(ctx: ServerContext, monkeypatch, raw: str):
    monkeypatch.setenv("FAKE_RECORDER_PERMISSION_RESULT", raw)
    args = arguments()
    outcome = dispatch(ctx, TOOL, args)
    assert outcome.is_error and outcome.payload["error"]["code"] == "recorder_unavailable"
    assert ctx.catalog.get_request(args["request_id"]) is None
    monkeypatch.delenv("FAKE_RECORDER_PERMISSION_RESULT")
    assert not dispatch(ctx, TOOL, arguments()).is_error  # explicit new-ID retry after completion


def test_output_validation_precedes_replay_cache(ctx: ServerContext):
    ctx.validate_output = False
    ctx.handlers = {**ctx.handlers, TOOL: lambda _ctx, _args: {"invalid": "output"}}
    args = arguments()
    outcome = dispatch(ctx, TOOL, args)
    assert outcome.is_error and outcome.payload["error"]["code"] == "contract_mismatch"
    assert ctx.catalog.get_request(args["request_id"]) is None


def test_fresh_diagnostics_uses_one_permission_snapshot(ctx: ServerContext, monkeypatch):
    assert dispatch(ctx, "get_server_info", {}).payload["capabilities"]["recording"]
    monkeypatch.setenv("FAKE_RECORDER_CHECK", json.dumps(DENIED))
    cached = dispatch(ctx, "get_server_info", {}).payload["capabilities"]
    assert cached["recording"] and cached["permissions"] == GRANTED
    refreshed = dispatch(ctx, "get_server_info", {"refresh_permissions": True}).payload[
        "capabilities"
    ]
    assert not refreshed["recording"] and refreshed["permissions"] == DENIED
    assert refreshed["permission_setup_in_progress"] is False


def test_server_info_does_not_recheck_when_calculating_capability(ctx: ServerContext, monkeypatch):
    reports = iter([GRANTED, DENIED])
    monkeypatch.setattr(recording, "_run_check", lambda _path: next(reports))
    one = dispatch(ctx, "get_server_info", {"refresh_permissions": True}).payload["capabilities"]
    two = dispatch(ctx, "get_server_info", {"refresh_permissions": True}).payload["capabilities"]
    assert one["permissions"] == GRANTED and one["recording"]
    assert two["permissions"] == DENIED and not two["recording"]


def test_cached_snapshot_keeps_busy_when_permission_finishes_before_info_response(
    ctx: ServerContext, monkeypatch
):
    """A fresh request served from the busy cache must not pair it with a later false flag."""
    current = {"report": GRANTED}
    monkeypatch.setattr(recording, "_run_check", lambda _path: current["report"])
    initial = ctx.recorder.permission_snapshot()
    assert initial.permissions == GRANTED and not initial.in_progress
    action_entered, action_release = threading.Event(), threading.Event()
    snapshot_captured, info_release = threading.Event(), threading.Event()
    observed = []

    def fake_action(_command, permission, action):
        action_entered.set()
        assert action_release.wait(5)
        current["report"] = DENIED
        return {
            "permission": permission,
            "action": action,
            "permissions": DENIED,
            "settings_opened": False,
        }

    real_snapshot = ctx.recorder.permission_snapshot

    def hold_snapshot(*, max_age=5):
        snapshot = real_snapshot(max_age=max_age)
        if snapshot.in_progress:
            observed.append(snapshot)
            snapshot_captured.set()
            assert info_release.wait(5)
        return snapshot

    monkeypatch.setattr(ctx.recorder._permission_setup, "run", fake_action)
    monkeypatch.setattr(ctx.recorder, "permission_snapshot", hold_snapshot)
    with ThreadPoolExecutor(max_workers=2) as executor:
        action = executor.submit(dispatch, ctx, TOOL, arguments())
        assert action_entered.wait(5)
        info = executor.submit(dispatch, ctx, "get_server_info", {"refresh_permissions": True})
        try:
            assert snapshot_captured.wait(5)
            action_release.set()
            assert not action.result(timeout=5).is_error
            assert not ctx.recorder.permission_setup_in_progress
        finally:
            action_release.set()
            info_release.set()
        outcome = info.result(timeout=5)
    assert not outcome.is_error
    caps = outcome.payload["capabilities"]
    assert caps["permissions"] == GRANTED and caps["recording"]
    assert caps["permission_setup_in_progress"] is True
    finished = real_snapshot(max_age=0)
    assert initial.revision < observed[0].revision < finished.revision
    assert finished.permissions == DENIED and not finished.in_progress
    refreshed = dispatch(ctx, "get_server_info", {"refresh_permissions": True}).payload[
        "capabilities"
    ]
    assert refreshed["permissions"] == DENIED and not refreshed["recording"]
    assert refreshed["permission_setup_in_progress"] is False


def test_read_only_diagnostics_never_requests_permissions(
    ctx: ServerContext, tmp_path: Path, monkeypatch
):
    marker = tmp_path / "permission.pid"
    monkeypatch.setenv("FAKE_RECORDER_PERMISSION_MARKER", str(marker))
    monkeypatch.setenv("FAKE_RECORDER_CHECK", json.dumps(DENIED))
    for args in ({}, {"refresh_permissions": True}):
        outcome = dispatch(ctx, "get_server_info", args)
        assert not outcome.is_error
        assert outcome.payload["capabilities"]["permission_setup_in_progress"] is False
    assert not marker.exists()


def test_context_close_reaps_pending_permission(home: Path, tmp_path: Path, monkeypatch):
    marker = tmp_path / "permission.pid"
    monkeypatch.setenv("FAKE_RECORDER_PERMISSION_MARKER", str(marker))
    monkeypatch.setenv("FAKE_RECORDER_PERMISSION_DELAY", "20")
    ctx = build_context(home)
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(dispatch, ctx, TOOL, arguments())
        pid = wait_marker(marker)
        ctx.close()
        outcome = pending.result(timeout=5)
    assert outcome.is_error and outcome.payload["error"]["code"] == "recorder_unavailable"
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    assert not ctx.recorder.permission_setup_in_progress


@pytest.mark.parametrize(
    "invalid", [{"permission": "camera"}, {"action": "exec"}, {"url": "https://example.test"}]
)
def test_invalid_permission_tool_arguments_rejected_before_spawn(
    ctx: ServerContext, monkeypatch, invalid: dict
):
    monkeypatch.setattr(
        ctx.recorder, "configure_permission", lambda *_a: pytest.fail("must not spawn")
    )
    outcome = dispatch(ctx, TOOL, arguments(**invalid))
    assert outcome.is_error and outcome.payload["error"]["code"] == "invalid_argument"
