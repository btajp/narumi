"""Profile tools, profile defaults on start_recording / import_recording and auto-export."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest
from conftest import PerCallClient, call, fake_process_meeting, wait_job
from narumi.bundle import Bundle
from narumi.errors import InvalidArgumentError
from narumi_server.context import ServerContext
from test_surface_tools import write_silence_wav  # shared helper (rootdir is on sys.path)


def rid() -> str:
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------- CRUD via tools
async def test_profile_crud(client: PerCallClient, ctx: ServerContext):
    listed = await call(client, "list_profiles")
    assert listed["default"] == "default"
    assert [p["name"] for p in listed["profiles"]] == ["default"]
    builtin = listed["profiles"][0]
    assert builtin["is_default"] is True
    assert builtin["config"]["transcription_engine"] == "auto"
    assert builtin["scope"] is None and builtin["engagement"] is None
    assert builtin["export_destinations"] == []

    created = await call(
        client,
        "set_profile",
        {
            "name": "customer",
            "config": {"transcription_engine": "fake", "self_name": "岡村"},
            "scope": "cloudnative",
            "engagement": "acme",
            "export_destinations": ["markdown"],
            "request_id": rid(),
        },
    )
    assert created["profile"]["name"] == "customer"
    assert created["profile"]["config"]["transcription_engine"] == "fake"
    assert created["profile"]["config"]["self_name"] == "岡村"
    assert created["profile"]["scope"] == "cloudnative"
    assert created["profile"]["export_destinations"] == ["markdown"]
    assert created["profile"]["is_default"] is False
    assert (ctx.data_root / "profiles.json").is_file()  # persisted source of truth

    got = await call(client, "get_profile", {"name": "customer"})
    assert got["profile"] == created["profile"]
    missing = await call(client, "get_profile", {"name": "nope"})
    assert missing["error"]["code"] == "not_found"

    # partial update: only the passed keys change; null clears
    updated = await call(
        client,
        "set_profile",
        {"name": "customer", "engagement": None, "make_default": True, "request_id": rid()},
    )
    assert updated["profile"]["engagement"] is None
    assert updated["profile"]["scope"] == "cloudnative"
    assert updated["profile"]["config"]["self_name"] == "岡村"
    assert updated["profile"]["is_default"] is True
    listed = await call(client, "list_profiles")
    assert listed["default"] == "customer"
    assert {p["name"]: p["is_default"] for p in listed["profiles"]} == {
        "default": False,
        "customer": True,
    }

    # the current default (and the built-in default) cannot be deleted
    still_default = await call(client, "delete_profile", {"name": "customer", "request_id": rid()})
    assert still_default["error"]["code"] == "invalid_argument"
    builtin_delete = await call(client, "delete_profile", {"name": "default", "request_id": rid()})
    assert builtin_delete["error"]["code"] == "invalid_argument"
    unknown = await call(client, "delete_profile", {"name": "ghost", "request_id": rid()})
    assert unknown["error"]["code"] == "not_found"

    await call(
        client, "set_profile", {"name": "default", "make_default": True, "request_id": rid()}
    )
    deleted = await call(client, "delete_profile", {"name": "customer", "request_id": rid()})
    assert deleted == {"name": "customer", "deleted": True}
    assert [p["name"] for p in (await call(client, "list_profiles"))["profiles"]] == ["default"]
    assert ctx.catalog.list_audit(action="set_profile")
    assert ctx.catalog.list_audit(action="delete_profile")


async def test_set_profile_validation(client: PerCallClient, ctx: ServerContext):
    policy = await call(
        client,
        "set_profile",
        {"name": "bad", "config": {"llm_provider": "anthropic-api"}, "request_id": rid()},
    )
    assert policy["error"]["code"] == "policy_violation"
    engine = await call(
        client,
        "set_profile",
        {"name": "bad", "config": {"transcription_engine": "whisper-x"}, "request_id": rid()},
    )
    assert engine["error"]["code"] == "engine_unavailable"
    destination = await call(
        client,
        "set_profile",
        {"name": "bad", "export_destinations": ["notion"], "request_id": rid()},
    )
    assert destination["error"]["code"] == "invalid_argument"
    assert destination["error"]["details"]["unknown"] == ["notion"]
    # nothing was persisted by the rejected calls
    assert (await call(client, "get_profile", {"name": "bad"}))["error"]["code"] == "not_found"


# ---------------------------------------------------------------------------- profile defaults
async def test_start_recording_applies_profile_defaults(client: PerCallClient, ctx: ServerContext):
    await call(
        client,
        "set_profile",
        {
            "name": "meet",
            "config": {"transcription_engine": "fake", "self_name": "岡村"},
            "scope": "cloudnative",
            "engagement": "acme",
            "request_id": rid(),
        },
    )
    started = await call(client, "start_recording", {"profile": "meet", "request_id": rid()})
    meeting_id = started["meeting_id"]
    manifest = Bundle.find(ctx.meetings_root, meeting_id).manifest
    assert manifest.profile == "meet"
    assert manifest.scope == "cloudnative"
    assert manifest.engagement == "acme"
    assert manifest.config.transcription_engine == "fake"
    assert manifest.config.self_name == "岡村"
    await call(client, "stop_recording", {"request_id": rid(), "auto_process": False})

    # explicit arguments and config keys win over the profile
    started = await call(
        client,
        "start_recording",
        {
            "profile": "meet",
            "scope": "btcon",
            "engagement": "other",
            "config": {"self_name": "私"},
            "request_id": rid(),
        },
    )
    manifest = Bundle.find(ctx.meetings_root, started["meeting_id"]).manifest
    assert manifest.scope == "btcon"
    assert manifest.engagement == "other"
    assert manifest.config.self_name == "私"
    assert manifest.config.transcription_engine == "fake"  # unset keys keep profile values
    await call(client, "stop_recording", {"request_id": rid(), "auto_process": False})

    # omitted profile = the profile marked is_default
    await call(client, "set_profile", {"name": "meet", "make_default": True, "request_id": rid()})
    started = await call(client, "start_recording", {"request_id": rid()})
    manifest = Bundle.find(ctx.meetings_root, started["meeting_id"]).manifest
    assert manifest.profile == "meet" and manifest.scope == "cloudnative"
    await call(client, "stop_recording", {"request_id": rid(), "auto_process": False})

    unknown = await call(client, "start_recording", {"profile": "vip", "request_id": rid()})
    assert unknown["error"]["code"] == "invalid_argument"


async def test_import_recording_applies_profile_defaults(
    client: PerCallClient, ctx: ServerContext, tmp_path: Path
):
    await call(
        client,
        "set_profile",
        {
            "name": "zoom",
            "config": {"transcription_engine": "fake"},
            "scope": "cloudnative",
            "request_id": rid(),
        },
    )
    mic = write_silence_wav(tmp_path / "mic.wav")
    result = await call(
        client,
        "import_recording",
        {
            "meeting_name": "取り込み",
            "mic_path": str(mic),
            "profile": "zoom",
            "auto_process": False,
            "request_id": rid(),
        },
    )
    manifest = Bundle.find(ctx.meetings_root, result["meeting_id"]).manifest
    assert manifest.profile == "zoom"
    assert manifest.scope == "cloudnative"
    assert manifest.config.transcription_engine == "fake"
    # the imported meeting is scoped: invisible without its scope selector
    denied = await call(client, "get_meeting", {"meeting_id": result["meeting_id"]})
    assert denied["error"]["code"] == "scope_denied"


# ---------------------------------------------------------------------------- auto-export
async def test_process_job_auto_exports_profile_destinations(
    client: PerCallClient, ctx: ServerContext, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr("narumi.pipeline.process_meeting", fake_process_meeting)
    await call(
        client,
        "set_profile",
        {
            "name": "auto-md",
            "export_destinations": ["markdown"],
            "make_default": True,
            "request_id": rid(),
        },
    )
    await call(client, "start_recording", {"request_id": rid()})
    stopped = await call(client, "stop_recording", {"request_id": rid()})
    meeting_id = stopped["meeting_id"]
    job = await wait_job(ctx, stopped["job_id"])
    assert job["status"] == "succeeded", job.get("error")
    exports = job["result"]["exports"]
    assert len(exports) == 1
    assert exports[0]["destination"] == "markdown"
    assert exports[0]["minutes_version"] == 1
    assert exports[0]["ref"].endswith(f"{meeting_id}-v1.md")
    assert Path(exports[0]["ref"]).is_file()
    assert "export_errors" not in job["result"]

    manifest = Bundle.find(ctx.meetings_root, meeting_id).manifest
    assert [(e.destination, e.minutes_version) for e in manifest.exports] == [("markdown", 1)]
    assert [e["destination"] for e in ctx.catalog.list_exports(meeting_id)] == ["markdown"]
    meeting = await call(client, "get_meeting", {"meeting_id": meeting_id})
    assert [e["destination"] for e in meeting["exports"]] == ["markdown"]


async def test_auto_export_failure_never_fails_the_job(
    client: PerCallClient, ctx: ServerContext, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr("narumi.pipeline.process_meeting", fake_process_meeting)

    def failing_export(bundle, destination, **kwargs: Any):
        raise InvalidArgumentError("disk full (simulated)", details={"destination": destination})

    monkeypatch.setattr("narumi.pipeline.export_meeting", failing_export)
    await call(
        client,
        "set_profile",
        {
            "name": "auto-md",
            "export_destinations": ["markdown"],
            "make_default": True,
            "request_id": rid(),
        },
    )
    await call(client, "start_recording", {"request_id": rid()})
    stopped = await call(client, "stop_recording", {"request_id": rid()})
    job = await wait_job(ctx, stopped["job_id"])
    assert job["status"] == "succeeded", job.get("error")  # auto-export failure is not fatal
    assert "exports" not in job["result"]
    assert job["result"]["export_errors"] == [
        {
            "destination": "markdown",
            "error": {
                "code": "invalid_argument",
                "message": "disk full (simulated)",
                "details": {"destination": "markdown"},
            },
        }
    ]
    meeting = await call(client, "get_meeting", {"meeting_id": stopped["meeting_id"]})
    assert meeting["meeting"]["status"] == "ready"
    assert meeting["exports"] == []


async def test_empty_destinations_export_nothing(
    client: PerCallClient, ctx: ServerContext, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr("narumi.pipeline.process_meeting", fake_process_meeting)
    await call(client, "start_recording", {"request_id": rid()})  # built-in default profile
    stopped = await call(client, "stop_recording", {"request_id": rid()})
    job = await wait_job(ctx, stopped["job_id"])
    assert job["status"] == "succeeded"
    assert "exports" not in job["result"] and "export_errors" not in job["result"]
    assert Bundle.find(ctx.meetings_root, stopped["meeting_id"]).manifest.exports == []
