"""Tools added for surface parity: recording status, minutes, search, import, discard,
delete, catalog rebuild and the get_server_info diagnostics block."""

from __future__ import annotations

import json
import threading
import uuid
import wave
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from conftest import PerCallClient, call, make_recorded_bundle, wait_job, write_fake_minutes
from narumi.bundle import Bundle, TrackRecord
from narumi.models import EngineInfo, Segment, Transcript
from narumi_server.context import ServerContext
from narumi_server.jobs import JobProgress

MEETING_A = "20260827T010000Z-0000000a"
MEETING_B = "20260827T020000Z-0000000b"

FAKE_CONFIG = {
    "transcription_engine": "fake",
    "diarization_engine": "fake",
    "llm_provider": "none",
}


def rid() -> str:
    return str(uuid.uuid4())


def write_silence_wav(path: Path, seconds: float = 1.0) -> Path:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(b"\x00\x00" * int(16000 * seconds))
    return path


def add_own_mic_transcript(bundle: Bundle) -> None:
    transcript = Transcript(
        source_id="own-mic",
        kind="own",
        track="mic",
        engine=EngineInfo(name="fake", version="1"),
        segments=[Segment(id="own-mic:0", start=0.0, end=1.0, text="こんにちは")],
    )
    bundle.run_stage(
        "transcripts/own-mic",
        inputs={"preprocess/audio/mic": "abc"},
        params={},
        producer=("fake", "1"),
        output="transcripts/own-mic.json",
        fn=lambda out: out.write_text(transcript.model_dump_json(), encoding="utf-8"),
    )


def add_tracks(bundle: Bundle) -> None:
    tracks_dir = bundle.dir("tracks")
    for name, file_name in (("screen", "screen.mp4"), ("mic", "mic.wav"), ("system", "system.wav")):
        (tracks_dir / file_name).write_bytes(b"x" * 10)
        bundle.manifest.recording.tracks[name] = TrackRecord(
            path=f"tracks/{file_name}", sha256="0" * 64, bytes=10, duration_sec=1.5
        )
    bundle.save()


# ---------------------------------------------------------------------------- recording status
async def test_get_recording_status(client: PerCallClient, ctx: ServerContext):
    idle = await call(client, "get_recording_status")
    assert idle == {"active": False}

    started = await call(
        client, "start_recording", {"meeting_name": "週次定例", "request_id": rid()}
    )
    status = await call(client, "get_recording_status")
    assert status["active"] is True
    assert status["meeting_id"] == started["meeting_id"]
    assert status["meeting_name"] == "週次定例"
    assert status["started_at"] == started["started_at"]
    assert status["elapsed_sec"] >= 0
    assert status["tracks"] == started["tracks"]

    await call(client, "stop_recording", {"request_id": rid(), "auto_process": False})
    assert await call(client, "get_recording_status") == {"active": False}


# ---------------------------------------------------------------------------- get_minutes
async def test_get_minutes(client: PerCallClient, ctx: ServerContext):
    bundle = make_recorded_bundle(ctx, meeting_id=MEETING_A)
    none_yet = await call(client, "get_minutes", {"meeting_id": MEETING_A})
    assert none_yet["error"]["code"] == "not_found"

    write_fake_minutes(bundle, "# v1\n")
    write_fake_minutes(bundle, "# v2\n")
    ctx.catalog.upsert_meeting(bundle)

    latest = await call(client, "get_minutes", {"meeting_id": MEETING_A})
    assert latest["meeting_id"] == MEETING_A
    assert latest["version"] == 2
    assert latest["markdown"] == "# v2\n"
    assert latest["provider"] == "none"
    assert latest["generated_at"]
    assert latest["unresolved_speakers"] == ["other"]
    assert latest["available_versions"] == [1, 2]

    first = await call(client, "get_minutes", {"meeting_id": MEETING_A, "version": 1})
    assert first["version"] == 1 and first["markdown"] == "# v1\n"
    assert first["available_versions"] == [1, 2]

    missing = await call(client, "get_minutes", {"meeting_id": MEETING_A, "version": 9})
    assert missing["error"]["code"] == "not_found"
    assert missing["error"]["details"]["available"] == [1, 2]
    unknown = await call(client, "get_minutes", {"meeting_id": MEETING_B})
    assert unknown["error"]["code"] == "not_found"

    scoped = make_recorded_bundle(ctx, meeting_id=MEETING_B, scope="cloudnative")
    write_fake_minutes(scoped, "# 極秘\n")
    ctx.catalog.upsert_meeting(scoped)
    denied = await call(client, "get_minutes", {"meeting_id": MEETING_B})
    assert denied["error"]["code"] == "scope_denied"
    ok = await call(client, "get_minutes", {"meeting_id": MEETING_B, "scope": "cloudnative"})
    assert ok["markdown"] == "# 極秘\n"


# ---------------------------------------------------------------------------- search_transcripts
async def test_search_transcripts(client: PerCallClient, ctx: ServerContext):
    plain = make_recorded_bundle(ctx, meeting_id=MEETING_A, name="社内定例")
    write_fake_minutes(plain)
    scoped = make_recorded_bundle(ctx, meeting_id=MEETING_B, name="顧客定例", scope="cloudnative")
    write_fake_minutes(scoped)
    for bundle in (plain, scoped):
        ctx.catalog.upsert_meeting(bundle)
        ctx.catalog.index_segments(bundle)

    hits = (await call(client, "search_transcripts", {"query": "オンボーディング"}))["hits"]
    assert [h["meeting_id"] for h in hits] == [MEETING_A]  # default deny hides the scoped one
    assert hits[0]["meeting_name"] == "社内定例"
    assert hits[0]["source_id"] == "merged"
    assert hits[0]["segment_id"] == "merged:0"
    assert hits[0]["speaker"] == "岡村"
    assert hits[0]["start"] == 0.0 and hits[0]["end"] == 1.5
    assert "オンボーディング" in hits[0]["text"]

    both = await call(
        client, "search_transcripts", {"query": "オンボーディング", "scope": "cloudnative"}
    )
    assert sorted(h["meeting_id"] for h in both["hits"]) == [MEETING_A, MEETING_B]
    limited = await call(
        client,
        "search_transcripts",
        {"query": "オンボーディング", "scope": "cloudnative", "limit": 1},
    )
    assert len(limited["hits"]) == 1

    assert (await call(client, "search_transcripts", {"query": "存在しない語"}))["hits"] == []
    bad = await call(client, "search_transcripts", {"query": ""})
    assert bad["error"]["code"] == "invalid_argument"


# ---------------------------------------------------------------------------- import_recording
async def test_import_recording(client: PerCallClient, ctx: ServerContext, tmp_path: Path):
    mic = write_silence_wav(tmp_path / "mic.wav")
    system = write_silence_wav(tmp_path / "system.wav")

    result = await call(
        client,
        "import_recording",
        {
            "meeting_name": "Zoom 取り込み",
            "mic_path": str(mic),
            "system_path": str(system),
            "started_at": "2026-08-25T09:00:00Z",
            "engagement": "acme",
            "auto_process": False,
            "request_id": rid(),
        },
    )
    assert "error" not in result, result
    meeting_id = result["meeting_id"]
    assert meeting_id.startswith("20260825T090000Z-")  # started_at drives the id
    assert Path(result["bundle_path"]) == ctx.meetings_root / meeting_id
    assert set(result["tracks"]) == {"mic", "system"}
    for track in result["tracks"].values():
        assert len(track["sha256"]) == 64
        assert track["bytes"] > 0
        assert track["duration_sec"] == pytest.approx(1.0, abs=0.2)  # ffprobe
        assert track["discarded"] is False
    assert "job_id" not in result

    manifest = Bundle.find(ctx.meetings_root, meeting_id).manifest
    assert manifest.status == "recorded"
    assert manifest.meeting_name == "Zoom 取り込み"
    assert manifest.engagement == "acme"
    assert manifest.recording.started_at == "2026-08-25T09:00:00Z"
    assert manifest.recording.stopped_at is not None
    assert (ctx.meetings_root / meeting_id / "tracks" / "mic.wav").is_file()
    listed = await call(client, "list_meetings")
    assert meeting_id in [m["meeting_id"] for m in listed["meetings"]]
    assert ctx.catalog.list_audit(action="import_recording")

    # idempotent replay: same request_id → same result, no second bundle
    key = rid()
    once = await call(
        client,
        "import_recording",
        {"meeting_name": "once", "mic_path": str(mic), "auto_process": False, "request_id": key},
    )
    again = await call(
        client,
        "import_recording",
        {"meeting_name": "once", "mic_path": str(mic), "auto_process": False, "request_id": key},
    )
    assert again == once
    assert len(list(ctx.meetings_root.iterdir())) == 2


async def test_import_recording_hardlink_and_started_at_default(
    client: PerCallClient, ctx: ServerContext, tmp_path: Path
):
    mic = write_silence_wav(tmp_path / "mic.wav")
    result = await call(
        client,
        "import_recording",
        {
            "meeting_name": "リンク取り込み",
            "mic_path": str(mic),
            "copy": False,
            "auto_process": False,
            "request_id": rid(),
        },
    )
    assert "error" not in result, result
    imported = ctx.meetings_root / result["meeting_id"] / "tracks" / "mic.wav"
    assert imported.stat().st_nlink == 2  # hardlinked, not copied
    # started_at omitted → derived from the oldest input file's mtime
    manifest = Bundle.find(ctx.meetings_root, result["meeting_id"]).manifest
    assert manifest.recording.started_at is not None
    derived = datetime.fromisoformat(manifest.recording.started_at).timestamp()
    assert abs(mic.stat().st_mtime - derived) < 2


async def test_import_recording_errors(client: PerCallClient, ctx: ServerContext, tmp_path: Path):
    missing = await call(
        client,
        "import_recording",
        {"meeting_name": "x", "mic_path": str(tmp_path / "nope.wav"), "request_id": rid()},
    )
    assert missing["error"]["code"] == "not_found"
    directory = await call(
        client,
        "import_recording",
        {"meeting_name": "x", "mic_path": str(tmp_path), "request_id": rid()},
    )
    assert directory["error"]["code"] == "invalid_argument"
    relative = await call(
        client,
        "import_recording",
        {"meeting_name": "x", "mic_path": "mic.wav", "request_id": rid()},
    )
    assert relative["error"]["code"] == "invalid_argument"  # schema pattern ^/
    neither = await call(client, "import_recording", {"meeting_name": "x", "request_id": rid()})
    assert neither["error"]["code"] == "invalid_argument"  # schema anyOf mic|system
    mic = write_silence_wav(tmp_path / "mic.wav")
    policy = await call(
        client,
        "import_recording",
        {
            "meeting_name": "x",
            "mic_path": str(mic),
            "config": {"llm_provider": "anthropic-api"},
            "request_id": rid(),
        },
    )
    assert policy["error"]["code"] == "policy_violation"
    profile = await call(
        client,
        "import_recording",
        {"meeting_name": "x", "mic_path": str(mic), "profile": "vip", "request_id": rid()},
    )
    assert profile["error"]["code"] == "invalid_argument"
    assert list(ctx.meetings_root.iterdir()) == []  # every rejection left nothing behind


async def test_import_recording_auto_process_e2e(
    client: PerCallClient, ctx: ServerContext, tmp_path: Path
):
    """Import → real pipeline job (ffmpeg + fake engines) → minutes → searchable."""
    mic = write_silence_wav(tmp_path / "mic.wav")
    system = write_silence_wav(tmp_path / "system.wav")
    result = await call(
        client,
        "import_recording",
        {
            "meeting_name": "E2E 取り込み",
            "mic_path": str(mic),
            "system_path": str(system),
            "config": FAKE_CONFIG,
            "request_id": rid(),
        },
    )
    assert "error" not in result, result
    job = await wait_job(ctx, result["job_id"], timeout=120.0)
    assert job["status"] == "succeeded", job.get("error")
    assert job["kind"] == "process"
    meeting = await call(client, "get_meeting", {"meeting_id": result["meeting_id"]})
    assert meeting["meeting"]["status"] == "ready"
    assert meeting["latest_minutes"]["version"] == 1
    minutes = await call(client, "get_minutes", {"meeting_id": result["meeting_id"]})
    assert minutes["version"] == 1 and minutes["markdown"]
    found = await call(client, "search_transcripts", {"query": "ダミー発話"})
    assert found["hits"] and {h["meeting_id"] for h in found["hits"]} == {result["meeting_id"]}


# ---------------------------------------------------------------------------- list active_job
async def test_list_meetings_reports_active_job(client: PerCallClient, ctx: ServerContext):
    make_recorded_bundle(ctx, meeting_id=MEETING_A)
    listed = await call(client, "list_meetings")
    assert listed["meetings"][0]["active_job"] is None

    gate = threading.Event()
    entered = threading.Event()

    def blocking(progress: JobProgress) -> dict[str, Any]:
        progress("transcribe", 0.4)
        entered.set()
        assert gate.wait(10)
        return {}

    job_id = ctx.jobs.submit("process", MEETING_A, blocking)
    try:
        assert entered.wait(10)
        listed = await call(client, "list_meetings")
        active = listed["meetings"][0]["active_job"]
        assert active == {
            "job_id": job_id,
            "kind": "process",
            "status": "running",
            "progress": {"stage": "transcribe", "fraction": 0.4},
        }
    finally:
        gate.set()
    await wait_job(ctx, job_id)
    listed = await call(client, "list_meetings")
    assert listed["meetings"][0]["active_job"] is None


# ---------------------------------------------------------------------------- discard_tracks
async def test_discard_tracks(client: PerCallClient, ctx: ServerContext):
    bundle = make_recorded_bundle(ctx, meeting_id=MEETING_A)
    add_tracks(bundle)
    ctx.catalog.upsert_meeting(bundle)

    # mic / system need their own-transcript first
    blocked = await call(
        client,
        "discard_tracks",
        {"meeting_id": MEETING_A, "tracks": ["mic"], "request_id": rid()},
    )
    assert blocked["error"]["code"] == "invalid_argument"
    assert (ctx.meetings_root / MEETING_A / "tracks" / "mic.wav").exists()

    # screen is discardable at any time; sha256 / duration stay, bytes are cleared
    result = await call(
        client,
        "discard_tracks",
        {"meeting_id": MEETING_A, "tracks": ["screen"], "request_id": rid()},
    )
    assert result["tracks"]["screen"] == {
        "path": "tracks/screen.mp4",
        "sha256": "0" * 64,
        "bytes": None,
        "duration_sec": 1.5,
        "discarded": True,
    }
    assert result["tracks"]["mic"]["discarded"] is False
    assert not (ctx.meetings_root / MEETING_A / "tracks" / "screen.mp4").exists()
    manifest = Bundle.find(ctx.meetings_root, MEETING_A).manifest
    assert manifest.recording.tracks["screen"].discarded is True
    assert manifest.recording.tracks["screen"].sha256 == "0" * 64

    # re-discard is a no-op; with the transcript present mic becomes discardable
    again = await call(
        client,
        "discard_tracks",
        {"meeting_id": MEETING_A, "tracks": ["screen"], "request_id": rid()},
    )
    assert again["tracks"]["screen"]["discarded"] is True
    # re-read before writing: the handler saved the bundle, our first handle is stale
    add_own_mic_transcript(Bundle.find(ctx.meetings_root, MEETING_A))
    ok = await call(
        client,
        "discard_tracks",
        {"meeting_id": MEETING_A, "tracks": ["mic", "screen"], "request_id": rid()},
    )
    assert ok["tracks"]["mic"]["discarded"] is True
    assert not (ctx.meetings_root / MEETING_A / "tracks" / "mic.wav").exists()
    # system still lacks its transcript
    still = await call(
        client,
        "discard_tracks",
        {"meeting_id": MEETING_A, "tracks": ["system"], "request_id": rid()},
    )
    assert still["error"]["code"] == "invalid_argument"

    audit = ctx.catalog.list_audit(action="discard_tracks")
    assert audit[0]["detail"]["meeting_id"] == MEETING_A
    assert audit[0]["detail"]["discarded"] == ["mic"]

    # a meeting without the requested track answers not_found
    make_recorded_bundle(ctx, meeting_id=MEETING_B)
    no_track = await call(
        client,
        "discard_tracks",
        {"meeting_id": MEETING_B, "tracks": ["screen"], "request_id": rid()},
    )
    assert no_track["error"]["code"] == "not_found"


async def test_discard_and_delete_are_busy_guarded(client: PerCallClient, ctx: ServerContext):
    bundle = make_recorded_bundle(ctx, meeting_id=MEETING_A)
    add_tracks(bundle)
    ctx.catalog.upsert_meeting(bundle)
    gate = threading.Event()
    entered = threading.Event()

    def blocking(progress: JobProgress) -> dict[str, Any]:
        entered.set()
        assert gate.wait(10)
        return {}

    job_id = ctx.jobs.submit("process", MEETING_A, blocking)
    try:
        assert entered.wait(10)
        busy = await call(
            client,
            "discard_tracks",
            {"meeting_id": MEETING_A, "tracks": ["screen"], "request_id": rid()},
        )
        assert busy["error"]["code"] == "busy"
        busy = await call(
            client,
            "delete_meeting",
            {"meeting_id": MEETING_A, "confirm": True, "request_id": rid()},
        )
        assert busy["error"]["code"] == "busy"
    finally:
        gate.set()
    await wait_job(ctx, job_id)

    # while recording, both are busy too
    started = await call(client, "start_recording", {"request_id": rid()})
    recording_id = started["meeting_id"]
    busy = await call(
        client,
        "delete_meeting",
        {"meeting_id": recording_id, "confirm": True, "request_id": rid()},
    )
    assert busy["error"]["code"] == "busy"
    await call(client, "stop_recording", {"request_id": rid(), "auto_process": False})


# ---------------------------------------------------------------------------- delete_meeting
async def test_delete_meeting(client: PerCallClient, ctx: ServerContext):
    bundle = make_recorded_bundle(ctx, meeting_id=MEETING_A, scope="cloudnative")
    write_fake_minutes(bundle)
    ctx.catalog.upsert_meeting(bundle)
    ctx.catalog.index_segments(bundle)

    denied = await call(
        client,
        "delete_meeting",
        {"meeting_id": MEETING_A, "confirm": True, "request_id": rid()},
    )
    assert denied["error"]["code"] == "scope_denied"
    unconfirmed = await call(
        client,
        "delete_meeting",
        {"meeting_id": MEETING_A, "scope": "cloudnative", "confirm": False, "request_id": rid()},
    )
    assert unconfirmed["error"]["code"] == "invalid_argument"
    assert (ctx.meetings_root / MEETING_A).is_dir()

    result = await call(
        client,
        "delete_meeting",
        {"meeting_id": MEETING_A, "scope": "cloudnative", "confirm": True, "request_id": rid()},
    )
    assert result["meeting_id"] == MEETING_A and result["deleted"] is True
    moved_to = Path(result["moved_to"])
    assert moved_to.parent == ctx.data_root / "trash"
    assert moved_to.name.startswith(f"{MEETING_A}-")
    assert (moved_to / "manifest.json").is_file()  # nothing was erased, only moved
    assert not (ctx.meetings_root / MEETING_A).exists()
    assert ctx.catalog.get_meeting_row(MEETING_A) is None
    empty = await call(
        client, "search_transcripts", {"query": "オンボーディング", "scope": "cloudnative"}
    )
    assert empty["hits"] == []
    gone = await call(client, "get_meeting", {"meeting_id": MEETING_A, "scope": "cloudnative"})
    assert gone["error"]["code"] == "not_found"
    audit = ctx.catalog.list_audit(action="delete_meeting")
    assert audit[0]["detail"] == {"meeting_id": MEETING_A, "moved_to": str(moved_to)}

    unknown = await call(
        client,
        "delete_meeting",
        {"meeting_id": MEETING_B, "confirm": True, "request_id": rid()},
    )
    assert unknown["error"]["code"] == "not_found"


# ---------------------------------------------------------------------------- rebuild_catalog
async def test_rebuild_catalog(client: PerCallClient, ctx: ServerContext):
    a = make_recorded_bundle(ctx, meeting_id=MEETING_A)
    write_fake_minutes(a)
    make_recorded_bundle(ctx, meeting_id=MEETING_B)
    broken = ctx.meetings_root / "20260827T030000Z-0000000c"
    broken.mkdir()
    (broken / "manifest.json").write_text("{not json", encoding="utf-8")
    job_id = ctx.catalog.create_job("process", MEETING_A)
    ctx.catalog.save_request("req-keep", "regenerate", MEETING_A, {"job_id": job_id})

    key = rid()
    result = await call(client, "rebuild_catalog", {"request_id": key})
    assert result["meetings"] == 2
    assert result["segments"] == 2  # write_fake_minutes indexes two merged segments
    assert len(result["errors"]) == 1 and "20260827T030000Z-0000000c" in result["errors"][0]

    listed = await call(client, "list_meetings")
    assert sorted(m["meeting_id"] for m in listed["meetings"]) == [MEETING_A, MEETING_B]
    # jobs / requests / audit survive the rebuild (they are not derivable from bundles)
    assert ctx.catalog.get_job(job_id) is not None
    assert ctx.catalog.get_request("req-keep") is not None
    assert any(row["action"] == "catalog_rebuild" for row in ctx.catalog.list_audit())
    # idempotent replay returns the stored result without rebuilding again
    assert await call(client, "rebuild_catalog", {"request_id": key}) == result


# ---------------------------------------------------------------------------- diagnostics
async def test_get_server_info_diagnostics(client: PerCallClient, ctx: ServerContext):
    info = await call(client, "get_server_info")
    diag = info["diagnostics"]
    assert diag["data_root"] == str(ctx.data_root)
    assert diag["meetings_root"] == str(ctx.meetings_root)
    assert diag["catalog_path"] == str(ctx.catalog.db_path)
    assert diag["contracts_dir"] == str(ctx.contracts.path)
    assert diag["recorder_path"] == str(ctx.recorder.recorder_path)  # the fake recorder
    for tool in ("ffmpeg", "ffprobe"):
        binary = diag[tool]
        assert binary is None or (binary["path"].startswith("/") and binary["version"])
    assert json.dumps(diag)  # plain JSON data


async def test_get_server_info_reports_missing_recorder(
    home: Path, monkeypatch: pytest.MonkeyPatch
):
    from narumi_server.app import dispatch
    from narumi_server.context import build_context

    monkeypatch.setenv("NARUMI_RECORDER", str(home / "does-not-exist"))
    ctx = build_context(home, transports=["in-memory"], validate_output=True)
    try:
        outcome = dispatch(ctx, "get_server_info", {})
        assert not outcome.is_error
        assert outcome.payload["diagnostics"]["recorder_path"] is None
    finally:
        ctx.close()
