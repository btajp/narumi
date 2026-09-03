"""End-to-end tool tests through the in-memory MCP client (no network, no real recorder)."""

from __future__ import annotations

import json
import threading
import uuid
from pathlib import Path
from typing import Any

import pytest
from conftest import (
    TRANSPORT,
    PerCallClient,
    call,
    fake_process_meeting,
    make_recorded_bundle,
    wait_job,
    write_fake_minutes,
)
from mcp.client import Client
from narumi.bundle import Bundle
from narumi.errors import ContractMismatchError, EngineUnavailableError
from narumi.pipeline import ExportResult, ProcessResult
from narumi_server.app import build_server, dispatch
from narumi_server.context import ServerContext, build_context

MEETING_A = "20260827T010000Z-0000000a"
MEETING_B = "20260827T020000Z-0000000b"


def rid() -> str:
    return str(uuid.uuid4())


def stored_sources(ctx: ServerContext, meeting_id: str) -> list[str]:
    """File names under ``context/sources`` (the directory appears with the first context)."""
    directory = ctx.meetings_root / meeting_id / "context" / "sources"
    return sorted(p.name for p in directory.iterdir()) if directory.is_dir() else []


def collect_refs(schema: Any) -> list[str]:
    refs: list[str] = []
    if isinstance(schema, dict):
        for key, value in schema.items():
            if key == "$ref" and isinstance(value, str):
                refs.append(value)
            else:
                refs.extend(collect_refs(value))
    elif isinstance(schema, list):
        for item in schema:
            refs.extend(collect_refs(item))
    return refs


# ---------------------------------------------------------------------------- discovery
async def test_single_session_flow(server, ctx: ServerContext):
    """One persistent in-process session: list_tools, then several calls."""
    async with Client(server) as session:
        listed = await session.list_tools()
        assert len(listed.tools) == len(ctx.contracts.tool_names())
        info = await session.call_tool("get_server_info", {})
        assert not info.is_error and info.structured_content["name"] == "narumi"
        started = await session.call_tool("start_recording", {"request_id": rid()})
        assert not started.is_error, started.structured_content
        stopped = await session.call_tool(
            "stop_recording", {"request_id": rid(), "auto_process": False}
        )
        assert not stopped.is_error
        assert stopped.structured_content["meeting_id"] == started.structured_content["meeting_id"]
        bad = await session.call_tool("get_meeting", {})
        assert bad.is_error and bad.structured_content["error"]["code"] == "invalid_argument"
        assert session.server_info is not None and session.server_info.name == "narumi"


async def test_list_tools_matches_contracts(client: PerCallClient, ctx: ServerContext):
    listed = await client.list_tools()
    by_name = {tool.name: tool for tool in listed.tools}
    assert list(by_name) == ctx.contracts.tool_names()
    assert len(by_name) == len(ctx.contracts.tool_names())
    for contract in ctx.contracts:
        tool = by_name[contract.name]
        assert tool.title == contract.title
        assert tool.description == contract.description
        assert tool.input_schema == contract.input_schema
        assert tool.output_schema == contract.output_schema
        for ref in collect_refs(tool.input_schema) + collect_refs(tool.output_schema or {}):
            assert ref.startswith("#/$defs/"), ref  # self-contained: no external $ref
        assert tool.annotations is not None
        assert tool.annotations.read_only_hint == contract.annotations["readOnlyHint"]
        assert tool.annotations.destructive_hint == contract.annotations["destructiveHint"]
        assert tool.annotations.idempotent_hint == contract.annotations["idempotentHint"]
        assert tool.annotations.open_world_hint == contract.annotations["openWorldHint"]


async def test_get_server_info(client: PerCallClient, ctx: ServerContext):
    info = await call(client, "get_server_info")
    assert info["name"] == "narumi"
    assert info["contract_version"] == ctx.contracts.contract_version
    caps = info["capabilities"]
    assert caps["recording"] is True  # fake recorder resolvable
    assert caps["transports"] == [TRANSPORT]
    assert "fake" in caps["transcription_engines"]
    assert "none" in caps["diarization_engines"]
    assert "none" in caps["llm_providers"]
    assert "claude-agent-sdk" not in caps["llm_providers"]
    assert "markdown" in caps["export_destinations"]


async def test_list_export_destinations(client: PerCallClient):
    result = await call(client, "list_export_destinations")
    names = {d["name"] for d in result["destinations"]}
    assert {"markdown", "html"} <= names


# ---------------------------------------------------------------------------- errors
async def test_invalid_arguments_return_error_envelope(client: PerCallClient):
    result = await client.call_tool("get_meeting", {"meeting_id": "nope"})
    assert result.is_error
    error = result.structured_content["error"]
    assert error["code"] == "invalid_argument"
    assert error["details"]["tool"] == "get_meeting"
    assert error["details"]["errors"][0]["path"] == "$.meeting_id"
    # the text block carries the same envelope for clients without structured content
    assert json.loads(result.content[0].text) == result.structured_content

    result = await client.call_tool("no_such_tool", {})
    assert result.is_error and result.structured_content["error"]["code"] == "invalid_argument"

    result = await client.call_tool("list_meetings", {"limit": 0})
    assert result.is_error and result.structured_content["error"]["code"] == "invalid_argument"


async def test_unexpected_exception_becomes_internal(ctx: ServerContext):
    def boom(_ctx: ServerContext, _args: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("kaboom")

    ctx.handlers = {**ctx.handlers, "get_server_info": boom}
    outcome = dispatch(ctx, "get_server_info", {})
    assert outcome.is_error
    assert outcome.payload["error"]["code"] == "internal"
    assert outcome.payload["error"]["details"]["exception"] == "RuntimeError"
    assert "kaboom" in outcome.payload["error"]["message"]


async def test_output_violation_is_contract_mismatch(ctx: ServerContext):
    ctx.handlers = {**ctx.handlers, "get_server_info": lambda _c, _a: {"name": "wrong"}}
    outcome = dispatch(ctx, "get_server_info", {})
    assert outcome.is_error and outcome.payload["error"]["code"] == "contract_mismatch"


def test_handler_registry_mismatch_fails_startup(ctx: ServerContext):
    handlers = dict(ctx.handlers)
    del handlers["get_job_status"]
    handlers["bogus"] = lambda _c, _a: {}
    ctx.handlers = handlers
    with pytest.raises(ContractMismatchError) as exc:
        build_server(ctx)
    assert exc.value.details == {
        "missing_handlers": ["get_job_status"],
        "unlisted_handlers": ["bogus"],
    }


async def test_get_job_status_not_found(client: PerCallClient):
    result = await call(client, "get_job_status", {"job_id": "job-000000000000"})
    assert result["error"]["code"] == "not_found"


async def test_stop_without_recording_is_not_found(client: PerCallClient):
    result = await call(client, "stop_recording", {"request_id": rid()})
    assert result["error"]["code"] == "not_found"


# ---------------------------------------------------------------------------- recording flow
async def test_recording_flow(client: PerCallClient, ctx: ServerContext):
    started = await call(
        client,
        "start_recording",
        {"meeting_name": "週次定例", "engagement": "acme", "request_id": rid()},
    )
    assert "error" not in started
    meeting_id = started["meeting_id"]
    assert set(started["tracks"]) == {"screen", "mic", "system"}
    assert started["tracks"]["mic"] == "tracks/mic.wav"  # file names come from the recorder
    assert Path(started["bundle_path"]) == ctx.meetings_root / meeting_id
    assert ctx.recorder.active_meeting_id == meeting_id

    busy = await call(client, "start_recording", {"request_id": rid()})
    assert busy["error"]["code"] == "busy"
    assert busy["error"]["details"]["meeting_id"] == meeting_id

    listed = await call(client, "list_meetings")
    assert [m["meeting_id"] for m in listed["meetings"]] == [meeting_id]
    assert listed["meetings"][0]["status"] == "recording"

    stopped = await call(client, "stop_recording", {"request_id": rid(), "auto_process": False})
    assert "error" not in stopped
    assert stopped["meeting_id"] == meeting_id
    assert "job_id" not in stopped
    assert stopped["duration_sec"] >= 0
    for name in ("mic", "system"):
        track = stopped["tracks"][name]
        assert track["discarded"] is False
        assert track["bytes"] > 0 and len(track["sha256"]) == 64
        assert track["duration_sec"] == 1.0
    assert ctx.recorder.active_meeting_id is None

    meeting = await call(client, "get_meeting", {"meeting_id": meeting_id})
    assert meeting["meeting"]["status"] == "recorded"
    assert meeting["meeting"]["engagement"] == "acme"
    assert meeting["recording"]["stopped_at"] == stopped["stopped_at"]
    assert meeting["recording"]["tracks"] == stopped["tracks"]
    assert meeting["latest_minutes"] is None
    assert meeting["artifacts"] == []
    assert (ctx.meetings_root / meeting_id / "tracks" / "mic.wav").is_file()
    assert (ctx.meetings_root / meeting_id / "logs" / "recorder.stderr.log").exists()

    transcript = await call(client, "get_transcript", {"meeting_id": meeting_id})
    assert transcript["error"]["code"] == "not_found"
    assert transcript["error"]["details"]["available_sources"] == []

    again = await call(client, "stop_recording", {"request_id": rid()})
    assert again["error"]["code"] == "not_found"


async def test_discard_video_and_no_auto_process(client: PerCallClient, ctx: ServerContext):
    started = await call(client, "start_recording", {"request_id": rid()})
    meeting_id = started["meeting_id"]
    stopped = await call(
        client,
        "stop_recording",
        {"request_id": rid(), "auto_process": False, "discard_video": True},
    )
    screen = stopped["tracks"]["screen"]
    assert screen == {
        "path": "tracks/screen.mp4",
        "sha256": None,
        "bytes": None,
        "duration_sec": None,
        "discarded": True,
    }
    assert not (ctx.meetings_root / meeting_id / "tracks" / "screen.mp4").exists()
    assert (ctx.meetings_root / meeting_id / "tracks" / "mic.wav").exists()


async def test_auto_process_job_success(
    client: PerCallClient, ctx: ServerContext, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr("narumi.pipeline.process_meeting", fake_process_meeting)
    started = await call(client, "start_recording", {"request_id": rid()})
    meeting_id = started["meeting_id"]
    stopped = await call(client, "stop_recording", {"request_id": rid()})
    job_id = stopped["job_id"]
    assert job_id.startswith("job-")

    job = await wait_job(ctx, job_id)
    assert job["status"] == "succeeded", job
    status = await call(client, "get_job_status", {"job_id": job_id})
    assert status["job"]["job_id"] == job_id
    assert status["job"]["kind"] == "process"
    assert status["job"]["meeting_id"] == meeting_id
    assert status["job"]["status"] == "succeeded"
    assert status["job"]["progress"] == {"stage": "generate", "fraction": 1.0}
    assert status["job"]["result"] == {
        "meeting_id": meeting_id,
        "minutes_version": 1,
        "stages": ["merged/merged", "minutes/v1"],
        "skipped": [],
        "unresolved_speakers": ["other"],
    }

    meeting = await call(client, "get_meeting", {"meeting_id": meeting_id})
    assert meeting["meeting"]["status"] == "ready"
    assert meeting["meeting"]["latest_minutes_version"] == 1
    assert meeting["latest_minutes"]["version"] == 1
    assert meeting["latest_minutes"]["markdown"].startswith("# 議事録")
    assert meeting["artifacts"] == ["merged/merged", "minutes/v1"]
    assert [v["version"] for v in meeting["minutes_versions"]] == [1]

    brief = await call(client, "get_meeting", {"meeting_id": meeting_id, "include_minutes": False})
    assert brief["latest_minutes"] is None

    transcript = await call(client, "get_transcript", {"meeting_id": meeting_id})
    assert transcript["source"] == "merged"
    assert transcript["available_sources"] == ["merged"]
    assert [s["speaker_name"] for s in transcript["segments"]] == ["岡村", None]
    assert transcript["speaker_map"] == {
        "me": {"name": "岡村", "confidence": 1.0},
        "other": {"name": None, "confidence": 0.0},
    }

    # the FTS index was refreshed by the job
    found = await call(client, "list_meetings", {"query": "オンボーディング資料"})
    assert [m["meeting_id"] for m in found["meetings"]] == [meeting_id]


async def test_process_job_failure_is_recorded(
    client: PerCallClient, ctx: ServerContext, monkeypatch: pytest.MonkeyPatch
):
    def failing(
        bundle: Bundle, *, force: bool = False, progress=None, gaia_client_factory=None
    ) -> ProcessResult:
        raise EngineUnavailableError("mlx-whisper is not installed", details={"engine": "mlx"})

    monkeypatch.setattr("narumi.pipeline.process_meeting", failing)
    await call(client, "start_recording", {"request_id": rid()})
    stopped = await call(client, "stop_recording", {"request_id": rid()})
    job = await wait_job(ctx, stopped["job_id"])
    assert job["status"] == "failed"
    assert job["error"] == {
        "code": "engine_unavailable",
        "message": "mlx-whisper is not installed",
        "details": {"engine": "mlx"},
    }
    meeting = await call(client, "get_meeting", {"meeting_id": stopped["meeting_id"]})
    assert meeting["meeting"]["status"] == "failed"

    # unexpected exceptions (e.g. the NotImplementedError of the pipeline stub) → internal
    def crashing(bundle: Bundle, **kwargs: Any) -> ProcessResult:
        raise NotImplementedError("wired later")

    # Patch BEFORE enqueuing: the job thread starts immediately, and the real refresh_meeting
    # would otherwise race the patch (it did on CI: ffmpeg + a real engine import timed out).
    monkeypatch.setattr("narumi.pipeline.process_meeting", crashing)
    monkeypatch.setattr("narumi.pipeline.refresh_meeting", crashing)
    regen = await call(
        client,
        "regenerate",
        {"meeting_id": stopped["meeting_id"], "request_id": rid()},
    )
    assert regen["meeting_id"] == stopped["meeting_id"]
    job = await wait_job(ctx, regen["job_id"])
    assert job["status"] == "failed"
    assert job["error"]["code"] == "internal"
    assert job["error"]["details"]["exception"] == "NotImplementedError"


async def test_recorder_missing(home: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("NARUMI_RECORDER", str(home / "does-not-exist"))
    ctx = build_context(home, transports=[TRANSPORT], validate_output=True)
    try:
        async with Client(build_server(ctx)) as client:
            info = await call(client, "get_server_info")
            assert info["capabilities"]["recording"] is False
            result = await call(client, "start_recording", {"request_id": rid()})
            assert result["error"]["code"] == "recorder_unavailable"
            assert str(home / "does-not-exist") in result["error"]["details"]["candidates"]
            assert list(ctx.meetings_root.iterdir()) == []  # no orphan bundle
    finally:
        ctx.close()


async def test_recorder_error_event(client: PerCallClient, ctx: ServerContext, monkeypatch):
    monkeypatch.setenv("FAKE_RECORDER_FAIL", "permission_denied")
    result = await call(client, "start_recording", {"request_id": rid()})
    assert result["error"]["code"] == "recorder_unavailable"
    assert result["error"]["details"]["recorder_code"] == "permission_denied"
    assert list(ctx.meetings_root.iterdir()) == []
    assert ctx.recorder.is_active is False


async def test_recorder_crash_on_stop_marks_meeting_failed(
    client: PerCallClient, ctx: ServerContext
):
    started = await call(client, "start_recording", {"request_id": rid()})
    # simulate a recorder crash mid-recording: kill it behind the controller's back
    proc = ctx.recorder._proc  # noqa: SLF001 - test hook
    assert proc is not None
    proc.kill()
    proc.wait()
    result = await call(client, "stop_recording", {"request_id": rid()})
    assert result["error"]["code"] == "recorder_unavailable"
    meeting = await call(client, "get_meeting", {"meeting_id": started["meeting_id"]})
    assert meeting["meeting"]["status"] == "failed"
    assert ctx.recorder.is_active is False
    # a new recording can start afterwards
    again = await call(client, "start_recording", {"request_id": rid()})
    assert "error" not in again
    await call(client, "stop_recording", {"request_id": rid(), "auto_process": False})


# ---------------------------------------------------------------------------- idempotency
async def test_idempotent_replay(client: PerCallClient, ctx: ServerContext):
    key = rid()
    first = await call(client, "start_recording", {"meeting_name": "once", "request_id": key})
    replay = await call(client, "start_recording", {"meeting_name": "once", "request_id": key})
    assert replay == first
    stop_key = rid()
    stopped = await call(client, "stop_recording", {"request_id": stop_key, "auto_process": False})
    assert "error" not in stopped
    # replaying start after the recording stopped still returns the original result and
    # does not start a second recording
    replay2 = await call(client, "start_recording", {"request_id": key})
    assert replay2 == first
    assert ctx.recorder.is_active is False
    assert len(list(ctx.meetings_root.iterdir())) == 1
    # and replaying stop does not raise not_found
    assert await call(client, "stop_recording", {"request_id": stop_key}) == stopped
    # the same key on a different tool is rejected
    other = await call(client, "regenerate", {"meeting_id": first["meeting_id"], "request_id": key})
    assert other["error"]["code"] == "invalid_argument"
    assert ctx.catalog.get_request(key)["tool"] == "start_recording"  # type: ignore[index]


async def test_failed_calls_are_not_cached(client: PerCallClient, ctx: ServerContext):
    key = rid()
    ctx.recorder._explicit_path = ctx.data_root / "missing"  # noqa: SLF001
    failed = await call(client, "start_recording", {"request_id": key})
    assert failed["error"]["code"] == "recorder_unavailable"
    assert ctx.catalog.get_request(key) is None


# ---------------------------------------------------------------------------- scope
async def test_scope_rules(client: PerCallClient, ctx: ServerContext):
    make_recorded_bundle(ctx, meeting_id=MEETING_A, name="社内", scope=None)
    make_recorded_bundle(ctx, meeting_id=MEETING_B, name="顧客", scope="cloudnative")

    denied = await call(client, "get_meeting", {"meeting_id": MEETING_B})
    assert denied["error"]["code"] == "scope_denied"
    assert denied["error"]["details"]["meeting_scope"] == "cloudnative"
    denied = await call(client, "get_transcript", {"meeting_id": MEETING_B, "scope": "btcon"})
    assert denied["error"]["code"] == "scope_denied"

    ok = await call(client, "get_meeting", {"meeting_id": MEETING_B, "scope": "cloudnative"})
    assert ok["meeting"]["scope"] == "cloudnative"
    ok = await call(client, "get_meeting", {"meeting_id": MEETING_A, "scope": "cloudnative"})
    assert ok["meeting"]["meeting_id"] == MEETING_A  # unscoped is always visible

    listed = await call(client, "list_meetings")
    assert [m["meeting_id"] for m in listed["meetings"]] == [MEETING_A]
    listed = await call(client, "list_meetings", {"scope": "cloudnative"})
    assert [m["meeting_id"] for m in listed["meetings"]] == [MEETING_B, MEETING_A]
    assert ctx.catalog.list_audit(action="cross_scope_read") == []

    listed = await call(client, "list_meetings", {"scope": ["cloudnative", "btcon"]})
    assert [m["meeting_id"] for m in listed["meetings"]] == [MEETING_B, MEETING_A]
    cross = await call(
        client, "get_meeting", {"meeting_id": MEETING_B, "scope": ["btcon", "cloudnative"]}
    )
    assert cross["meeting"]["meeting_id"] == MEETING_B
    audit = ctx.catalog.list_audit(action="cross_scope_read")
    assert [a["detail"]["action"] for a in audit] == ["check_scope", "list_meetings"]
    assert audit[0]["detail"]["meeting_id"] == MEETING_B

    listed = await call(
        client,
        "list_meetings",
        {"range": {"from": "2026-08-27T01:30:00Z"}, "scope": "cloudnative", "limit": 5},
    )
    assert [m["meeting_id"] for m in listed["meetings"]] == [MEETING_B]
    bad = await call(
        client,
        "list_meetings",
        {"range": {"from": "2026-08-27T02:00:00Z", "to": "2026-08-27T01:00:00Z"}},
    )
    assert bad["error"]["code"] == "invalid_argument"


# ---------------------------------------------------------------------------- context / config
async def test_register_context(client: PerCallClient, ctx: ServerContext, tmp_path: Path):
    bundle = make_recorded_bundle(ctx, meeting_id=MEETING_A)
    result = await call(
        client,
        "register_context",
        {
            "meeting_id": MEETING_A,
            "source_type": "notion_ai_minutes",
            "content": "岡村: では定例を始めます。",
            "label": "Notion AI 議事録",
            "request_id": rid(),
        },
    )
    assert result["status"] == "parsed" and "job_id" not in result
    context_id = result["context_id"]
    source_path = bundle.path / "context" / "sources" / f"{context_id}.json"
    stored = json.loads(source_path.read_text(encoding="utf-8"))
    assert stored["content"] == "岡村: では定例を始めます。"
    assert stored["source_type"] == "notion_ai_minutes"
    assert stored["label"] == "Notion AI 議事録"

    agenda = tmp_path / "agenda.md"
    agenda.write_text("# agenda\n", encoding="utf-8")
    from_file = await call(
        client,
        "register_context",
        {
            "meeting_id": MEETING_A,
            "source_type": "document",
            "file_path": str(agenda),
            "request_id": rid(),
        },
    )
    stored = json.loads(
        (bundle.path / "context" / "sources" / f"{from_file['context_id']}.json").read_text(
            encoding="utf-8"
        )
    )
    assert stored["content"] == "# agenda\n" and stored["file_path"] == str(agenda)

    missing = await call(
        client,
        "register_context",
        {
            "meeting_id": MEETING_A,
            "source_type": "file",
            "file_path": str(tmp_path / "nope.md"),
            "request_id": rid(),
        },
    )
    assert missing["error"]["code"] == "not_found"

    two = await call(
        client,
        "register_context",
        {
            "meeting_id": MEETING_A,
            "source_type": "text",
            "content": "a",
            "url": "https://example.com/x",
            "request_id": rid(),
        },
    )
    assert two["error"]["code"] == "invalid_argument"

    meeting = await call(client, "get_meeting", {"meeting_id": MEETING_A})
    assert [c["context_id"] for c in meeting["contexts"]] == [context_id, from_file["context_id"]]
    assert meeting["contexts"][0]["label"] == "Notion AI 議事録"
    assert meeting["contexts"][1]["label"] is None
    rows = ctx.catalog.list_contexts(MEETING_A)
    assert [r["context_id"] for r in rows] == [context_id, from_file["context_id"]]


async def test_register_context_auto_regenerate(
    client: PerCallClient, ctx: ServerContext, monkeypatch: pytest.MonkeyPatch
):
    make_recorded_bundle(ctx, meeting_id=MEETING_A)
    seen: dict[str, Any] = {}

    def fake_regenerate(
        bundle,
        *,
        force=False,
        progress=None,
        reason="regenerate",
        job_id=None,
        gaia_client_factory=None,
    ):
        seen.update(
            force=force, reason=reason, job_id=job_id, contexts=len(bundle.manifest.contexts)
        )
        version = write_fake_minutes(bundle)
        return ProcessResult(meeting_id=bundle.meeting_id, minutes_version=version)

    monkeypatch.setattr("narumi.pipeline.refresh_meeting", fake_regenerate)
    result = await call(
        client,
        "register_context",
        {
            "meeting_id": MEETING_A,
            "source_type": "url",
            "url": "https://www.notion.so/example",
            "request_id": rid(),
            "auto_regenerate": True,
        },
    )
    job = await wait_job(ctx, result["job_id"])
    assert job["status"] == "succeeded"
    assert seen == {
        "force": False,
        "reason": f"register_context {result['context_id']}",
        "job_id": result["job_id"],
        "contexts": 1,
    }


async def test_set_meeting_config(client: PerCallClient, ctx: ServerContext):
    make_recorded_bundle(ctx, meeting_id=MEETING_A, scope="cloudnative")
    # a scoped meeting is default-deny for writes too: no selector → scope_denied, unchanged
    denied = await call(
        client,
        "set_meeting_config",
        {"meeting_id": MEETING_A, "request_id": rid(), "self_name": "岡村"},
    )
    assert denied["error"]["code"] == "scope_denied"
    assert Bundle.find(ctx.meetings_root, MEETING_A).manifest.config.self_name is None
    result = await call(
        client,
        "set_meeting_config",
        {
            "meeting_id": MEETING_A,
            "scope": "cloudnative",
            "request_id": rid(),
            "self_name": "岡村",
            "vocab_hints": ["gaia-library", "narumi"],
            "transcription_engine": "fake",
        },
    )
    assert result["scope"] == "cloudnative"
    assert result["config"]["self_name"] == "岡村"
    assert result["config"]["vocab_hints"] == ["gaia-library", "narumi"]
    assert result["config"]["transcription_engine"] == "fake"
    assert result["config"]["external_send_policy"] == "local_only"
    reopened = Bundle.find(ctx.meetings_root, MEETING_A)
    assert reopened.manifest.config.self_name == "岡村"

    # clearing the scope requires covering the current one (``scope``) and is audited old → new
    denied = await call(
        client,
        "set_meeting_config",
        {"meeting_id": MEETING_A, "request_id": rid(), "new_scope": None},
    )
    assert denied["error"]["code"] == "scope_denied"
    assert Bundle.find(ctx.meetings_root, MEETING_A).manifest.scope == "cloudnative"
    cleared = await call(
        client,
        "set_meeting_config",
        {
            "meeting_id": MEETING_A,
            "scope": "cloudnative",
            "request_id": rid(),
            "self_name": None,
            "new_scope": None,
        },
    )
    assert cleared["config"]["self_name"] is None
    assert cleared["scope"] is None
    assert Bundle.find(ctx.meetings_root, MEETING_A).manifest.scope is None
    assert ctx.catalog.get_meeting_row(MEETING_A)["scope"] is None  # type: ignore[index]
    audit = ctx.catalog.list_audit(action="set_meeting_config")
    assert audit[0]["detail"]["scope_changed"] is True
    assert (audit[0]["detail"]["scope_from"], audit[0]["detail"]["scope_to"]) == (
        "cloudnative",
        None,
    )
    # unscoped now: readable / writable without a selector, and can be scoped again
    rescoped = await call(
        client,
        "set_meeting_config",
        {"meeting_id": MEETING_A, "request_id": rid(), "new_scope": "btcon"},
    )
    assert rescoped["scope"] == "btcon"
    assert (await call(client, "get_meeting", {"meeting_id": MEETING_A}))["error"]["code"] == (
        "scope_denied"
    )
    back = await call(
        client,
        "set_meeting_config",
        {
            "meeting_id": MEETING_A,
            "scope": ["btcon", "cloudnative"],
            "request_id": rid(),
            "new_scope": None,
        },
    )
    assert back["scope"] is None

    violation = await call(
        client,
        "set_meeting_config",
        {"meeting_id": MEETING_A, "request_id": rid(), "llm_provider": "anthropic-api"},
    )
    assert violation["error"]["code"] == "policy_violation"
    assert Bundle.find(ctx.meetings_root, MEETING_A).manifest.config.llm_provider == "none"

    allowed = await call(
        client,
        "set_meeting_config",
        {
            "meeting_id": MEETING_A,
            "request_id": rid(),
            "llm_provider": "anthropic-api",
            "external_send_policy": "api_ok",
        },
    )
    assert allowed["config"]["llm_provider"] == "anthropic-api"

    unknown = await call(
        client,
        "set_meeting_config",
        {"meeting_id": MEETING_A, "request_id": rid(), "transcription_engine": "whisper-x"},
    )
    assert unknown["error"]["code"] == "engine_unavailable"

    bad_policy = await call(
        client,
        "set_meeting_config",
        {"meeting_id": MEETING_A, "request_id": rid(), "external_send_policy": "everything"},
    )
    assert bad_policy["error"]["code"] == "invalid_argument"

    missing = await call(
        client,
        "set_meeting_config",
        {"meeting_id": MEETING_B, "request_id": rid(), "language": "en"},
    )
    assert missing["error"]["code"] == "not_found"


async def test_start_recording_config_policy(client: PerCallClient, ctx: ServerContext):
    result = await call(
        client,
        "start_recording",
        {
            "request_id": rid(),
            "config": {"llm_provider": "anthropic-api", "external_send_policy": "local_only"},
        },
    )
    assert result["error"]["code"] == "policy_violation"
    assert list(ctx.meetings_root.iterdir()) == []
    bad_profile = await call(client, "start_recording", {"request_id": rid(), "profile": "vip"})
    assert bad_profile["error"]["code"] == "invalid_argument"


# ---------------------------------------------------------------------------- regenerate / export
async def test_regenerate_busy_and_success(
    client: PerCallClient, ctx: ServerContext, monkeypatch: pytest.MonkeyPatch
):
    make_recorded_bundle(ctx, meeting_id=MEETING_A)
    gate = threading.Event()

    def blocking_regenerate(
        bundle,
        *,
        force=False,
        progress=None,
        reason="regenerate",
        job_id=None,
        gaia_client_factory=None,
    ):
        progress("align", 0.1)
        assert gate.wait(10)
        version = write_fake_minutes(bundle)
        return ProcessResult(
            meeting_id=bundle.meeting_id, minutes_version=version, stages=["minutes/v1"]
        )

    monkeypatch.setattr("narumi.pipeline.refresh_meeting", blocking_regenerate)
    first = await call(
        client,
        "regenerate",
        {"meeting_id": MEETING_A, "request_id": rid(), "force": True, "reason": "test"},
    )
    assert first["meeting_id"] == MEETING_A
    busy = await call(client, "regenerate", {"meeting_id": MEETING_A, "request_id": rid()})
    assert busy["error"]["code"] == "busy"
    running = await call(client, "get_job_status", {"job_id": first["job_id"]})
    assert running["job"]["status"] in {"queued", "running"}
    gate.set()
    job = await wait_job(ctx, first["job_id"])
    assert job["status"] == "succeeded"
    assert job["result"]["minutes_version"] == 1
    meeting = await call(client, "get_meeting", {"meeting_id": MEETING_A})
    assert meeting["meeting"]["status"] == "ready"

    # a meeting that is still being recorded cannot be regenerated
    started = await call(client, "start_recording", {"request_id": rid()})
    blocked = await call(
        client, "regenerate", {"meeting_id": started["meeting_id"], "request_id": rid()}
    )
    assert blocked["error"]["code"] == "busy"
    await call(client, "stop_recording", {"request_id": rid(), "auto_process": False})


async def test_export_minutes(
    client: PerCallClient, ctx: ServerContext, monkeypatch: pytest.MonkeyPatch
):
    bundle = make_recorded_bundle(ctx, meeting_id=MEETING_A)
    none_yet = await call(
        client,
        "export_minutes",
        {"meeting_id": MEETING_A, "destination": "markdown", "request_id": rid()},
    )
    assert none_yet["error"]["code"] == "not_found"

    write_fake_minutes(bundle)
    write_fake_minutes(bundle, "# v2\n")
    ctx.catalog.upsert_meeting(bundle)
    calls: list[dict[str, Any]] = []

    def fake_export(
        bundle,
        destination,
        *,
        options=None,
        minutes_version=None,
        request_id=None,
        gaia_client_factory=None,
    ):
        calls.append(
            {
                "destination": destination,
                "options": options,
                "minutes_version": minutes_version,
                "request_id": request_id,
            }
        )
        from narumi.bundle import ExportRecord, utc_now_iso

        at = utc_now_iso()
        ref = f"/tmp/out-v{minutes_version}.{destination}"
        bundle.manifest.exports.append(
            ExportRecord(destination=destination, ref=ref, minutes_version=minutes_version, at=at)
        )
        bundle.save()
        return ExportResult(
            destination=destination, ref=ref, minutes_version=minutes_version, at=at
        )

    monkeypatch.setattr("narumi.pipeline.export_meeting", fake_export)
    sync = await call(
        client,
        "export_minutes",
        {
            "meeting_id": MEETING_A,
            "destination": "markdown",
            "options": {"output_path": "/tmp/x.md"},
            "request_id": rid(),
        },
    )
    assert "job_id" not in sync
    assert sync["result"]["destination"] == "markdown"
    assert sync["result"]["minutes_version"] == 2  # latest by default
    assert calls[-1]["options"] == {"output_path": "/tmp/x.md"}
    assert calls[-1]["minutes_version"] == 2

    # options are validated against the destination's options_schema before anything runs
    for options in ({"path": "/tmp/x.md"}, {"output_path": 1}, {"overwrite": "yes"}):
        rejected = await call(
            client,
            "export_minutes",
            {
                "meeting_id": MEETING_A,
                "destination": "markdown",
                "options": options,
                "request_id": rid(),
            },
        )
        assert rejected["error"]["code"] == "invalid_argument", options
        assert rejected["error"]["details"]["destination"] == "markdown"
        assert rejected["error"]["details"]["errors"]
    assert len(calls) == 1

    unknown = await call(
        client,
        "export_minutes",
        {"meeting_id": MEETING_A, "destination": "nope", "request_id": rid()},
    )
    assert unknown["error"]["code"] == "not_found"
    bad_version = await call(
        client,
        "export_minutes",
        {"meeting_id": MEETING_A, "destination": "html", "minutes_version": 9, "request_id": rid()},
    )
    assert bad_version["error"]["code"] == "not_found"

    queued = await call(
        client,
        "export_minutes",
        {
            "meeting_id": MEETING_A,
            "destination": "html",
            "minutes_version": 1,
            "request_id": rid(),
            "run_async": True,
        },
    )
    assert "result" not in queued
    job = await wait_job(ctx, queued["job_id"])
    assert job["status"] == "succeeded"
    assert job["kind"] == "export"
    assert job["result"]["destination"] == "html" and job["result"]["minutes_version"] == 1

    meeting = await call(client, "get_meeting", {"meeting_id": MEETING_A})
    assert [(e["destination"], e["minutes_version"]) for e in meeting["exports"]] == [
        ("markdown", 2),
        ("html", 1),
    ]
    assert len(ctx.catalog.list_exports(MEETING_A)) == 2


async def test_get_transcript_own_source(client: PerCallClient, ctx: ServerContext):
    bundle = make_recorded_bundle(ctx, meeting_id=MEETING_A)
    from narumi.models import EngineInfo, Segment, Transcript

    transcript = Transcript(
        source_id="own-mic",
        kind="own",
        track="mic",
        engine=EngineInfo(name="fake", version="1"),
        segments=[Segment(id="own-mic:0", start=0.0, end=1.0, text="こんにちは", confidence=0.9)],
    )
    bundle.run_stage(
        "transcripts/own-mic",
        inputs={"preprocess/audio/mic": "abc"},
        params={},
        producer=("fake", "1"),
        output="transcripts/own-mic.json",
        fn=lambda out: out.write_text(transcript.model_dump_json(), encoding="utf-8"),
    )
    result = await call(client, "get_transcript", {"meeting_id": MEETING_A, "source": "own-mic"})
    assert result["available_sources"] == ["own-mic"]
    assert result["segments"] == [
        {
            "id": "own-mic:0",
            "start": 0.0,
            "end": 1.0,
            "text": "こんにちは",
            "speaker": None,
            "confidence": 0.9,
        }
    ]
    assert result["speaker_map"] == {}
    missing = await call(
        client, "get_transcript", {"meeting_id": MEETING_A, "source": "own-system"}
    )
    assert missing["error"]["code"] == "not_found"
    assert missing["error"]["details"]["available_sources"] == ["own-mic"]


# ---------------------------------------------------------------------------- scope on writes
async def test_write_tools_enforce_scope(client: PerCallClient, ctx: ServerContext, tmp_path):
    """A read-only tool must never deny what a write tool hands out (default deny everywhere)."""
    bundle = make_recorded_bundle(ctx, meeting_id=MEETING_B, name="顧客", scope="secret")
    write_fake_minutes(bundle, "# 極秘\n")
    ctx.catalog.upsert_meeting(bundle)
    out = tmp_path / "leak.md"
    denied = await call(
        client,
        "export_minutes",
        {
            "meeting_id": MEETING_B,
            "destination": "markdown",
            "options": {"output_path": str(out)},
            "request_id": rid(),
        },
    )
    assert denied["error"]["code"] == "scope_denied" and not out.exists()
    denied = await call(client, "regenerate", {"meeting_id": MEETING_B, "request_id": rid()})
    assert denied["error"]["code"] == "scope_denied"
    denied = await call(
        client,
        "register_context",
        {"meeting_id": MEETING_B, "source_type": "text", "content": "x", "request_id": rid()},
    )
    assert denied["error"]["code"] == "scope_denied"
    assert Bundle.find(ctx.meetings_root, MEETING_B).manifest.contexts == []
    assert stored_sources(ctx, MEETING_B) == []

    exported = await call(
        client,
        "export_minutes",
        {
            "meeting_id": MEETING_B,
            "scope": "secret",
            "destination": "markdown",
            "options": {"output_path": str(out)},
            "request_id": rid(),
        },
    )
    assert exported["result"]["ref"] == str(out.resolve()) and out.read_text() == "# 極秘\n"
    registered = await call(
        client,
        "register_context",
        {
            "meeting_id": MEETING_B,
            "scope": ["secret", "other"],  # cross-scope selectors are audited
            "source_type": "text",
            "content": "x",
            "request_id": rid(),
        },
    )
    assert registered["status"] == "stored"
    cross = ctx.catalog.list_audit(action="cross_scope_read")
    assert cross and cross[0]["detail"]["meeting_id"] == MEETING_B


# ---------------------------------------------------------------------------- write locks
async def test_manifest_writes_are_rejected_while_a_job_runs(
    client: PerCallClient, ctx: ServerContext, monkeypatch: pytest.MonkeyPatch
):
    """Handlers must not save a manifest underneath a running job (and vice versa)."""
    bundle = make_recorded_bundle(ctx, meeting_id=MEETING_A)
    write_fake_minutes(bundle)
    ctx.catalog.upsert_meeting(bundle)
    entered = threading.Event()
    gate = threading.Event()

    def blocking_refresh(
        bundle,
        *,
        force=False,
        progress=None,
        reason="regenerate",
        job_id=None,
        gaia_client_factory=None,
    ):
        entered.set()
        assert gate.wait(10)
        version = write_fake_minutes(bundle)  # the job's in-memory bundle is saved here
        return ProcessResult(meeting_id=bundle.meeting_id, minutes_version=version)

    monkeypatch.setattr("narumi.pipeline.refresh_meeting", blocking_refresh)
    started = await call(client, "regenerate", {"meeting_id": MEETING_A, "request_id": rid()})
    assert entered.wait(10)
    try:
        registered = await call(
            client,
            "register_context",
            {
                "meeting_id": MEETING_A,
                "source_type": "text",
                "content": "途中で登録",
                "request_id": rid(),
            },
        )
        assert registered["error"]["code"] == "busy"
        configured = await call(
            client,
            "set_meeting_config",
            {"meeting_id": MEETING_A, "request_id": rid(), "self_name": "岡村"},
        )
        assert configured["error"]["code"] == "busy"
        exported = await call(
            client,
            "export_minutes",
            {"meeting_id": MEETING_A, "destination": "markdown", "request_id": rid()},
        )
        assert exported["error"]["code"] == "busy"
        exported = await call(
            client,
            "export_minutes",
            {
                "meeting_id": MEETING_A,
                "destination": "markdown",
                "request_id": rid(),
                "run_async": True,
            },
        )
        assert exported["error"]["code"] == "busy"
    finally:
        gate.set()
    job = await wait_job(ctx, started["job_id"])
    assert job["status"] == "succeeded" and job["result"]["minutes_version"] == 2
    manifest = Bundle.find(ctx.meetings_root, MEETING_A).manifest
    assert manifest.status == "ready" and manifest.latest_minutes_version == 2
    assert manifest.contexts == [] and manifest.config.self_name is None
    assert stored_sources(ctx, MEETING_A) == []
    assert ctx.catalog.list_contexts(MEETING_A) == []
    # once the job is done every write goes through and survives
    registered = await call(
        client,
        "register_context",
        {"meeting_id": MEETING_A, "source_type": "text", "content": "後で", "request_id": rid()},
    )
    assert registered["status"] == "stored"
    assert [c.context_id for c in Bundle.find(ctx.meetings_root, MEETING_A).manifest.contexts] == [
        registered["context_id"]
    ]


async def test_register_context_busy_leaves_no_side_effect(
    client: PerCallClient, ctx: ServerContext
):
    """busy (auto_regenerate while recording) must be decided before the context is stored."""
    started = await call(client, "start_recording", {"request_id": rid()})
    meeting_id = started["meeting_id"]
    key = rid()
    args = {
        "meeting_id": meeting_id,
        "source_type": "text",
        "content": "Notion transcript",
        "request_id": key,
        "auto_regenerate": True,
    }
    busy = await call(client, "register_context", args)
    assert busy["error"]["code"] == "busy"
    assert Bundle.find(ctx.meetings_root, meeting_id).manifest.contexts == []
    assert stored_sources(ctx, meeting_id) == []
    # without auto_regenerate a context may be attached while the meeting is still recording
    plain = await call(client, "register_context", {**args, "auto_regenerate": False})
    assert plain["status"] == "stored"
    await call(client, "stop_recording", {"request_id": rid(), "auto_process": False})
    contexts = Bundle.find(ctx.meetings_root, meeting_id).manifest.contexts
    assert [c.context_id for c in contexts] == [plain["context_id"]]


async def test_register_context_file_limits(client: PerCallClient, ctx: ServerContext, tmp_path):
    make_recorded_bundle(ctx, meeting_id=MEETING_A)

    async def attempt(file_path: str) -> dict[str, Any]:
        return await call(
            client,
            "register_context",
            {
                "meeting_id": MEETING_A,
                "source_type": "file",
                "file_path": file_path,
                "request_id": rid(),
            },
        )

    hidden_dir = tmp_path / ".ssh"
    hidden_dir.mkdir()
    (hidden_dir / "id_ed25519").write_text("PRIVATE KEY", encoding="utf-8")
    assert (await attempt(str(hidden_dir / "id_ed25519")))["error"]["code"] == "invalid_argument"
    link = tmp_path / "agenda.md"
    link.symlink_to(hidden_dir / "id_ed25519")
    assert (await attempt(str(link)))["error"]["code"] == "invalid_argument"
    assert (await attempt("~/agenda.md"))["error"]["code"] == "invalid_argument"
    assert (await attempt(str(tmp_path)))["error"]["code"] == "invalid_argument"
    big = tmp_path / "big.bin"
    with big.open("wb") as handle:
        handle.truncate(16 * 1024 * 1024 + 1)
    too_big = await attempt(str(big))
    assert too_big["error"]["code"] == "invalid_argument"
    assert too_big["error"]["details"]["max_bytes"] == 16 * 1024 * 1024
    assert Bundle.find(ctx.meetings_root, MEETING_A).manifest.contexts == []
    ok = tmp_path / "notes.txt"
    ok.write_text("議題\n", encoding="utf-8")
    assert (await attempt(str(ok)))["status"] == "stored"


async def test_stop_recording_discard_is_audited_and_ordered(
    client: PerCallClient, ctx: ServerContext
):
    started = await call(client, "start_recording", {"request_id": rid()})
    stopped = await call(
        client,
        "stop_recording",
        {"request_id": rid(), "auto_process": False, "discard_video": True},
    )
    assert stopped["tracks"]["screen"]["discarded"] is True
    audit = ctx.catalog.list_audit(action="stop_recording")
    assert audit[0]["detail"] == {
        "meeting_id": started["meeting_id"],
        "discard_video": True,
        "recorder_error": None,
    }


def test_context_close_finalizes_a_running_recording(home: Path):
    """Ctrl+C / launchd restart must not leave the recorder to be SIGKILLed mid-finalize."""
    ctx = build_context(home, transports=[TRANSPORT], validate_output=True)
    started = dispatch(ctx, "start_recording", {"request_id": rid()})
    assert not started.is_error, started.payload
    meeting_id = started.payload["meeting_id"]
    ctx.close()
    manifest = Bundle.find(ctx.meetings_root, meeting_id).manifest
    assert manifest.status == "recorded"
    assert manifest.recording.stopped_at is not None
    assert manifest.recording.tracks["mic"].sha256 is not None
    assert (ctx.meetings_root / meeting_id / "tracks" / "recorder.json").is_file()
    assert not ctx.recorder.is_active
