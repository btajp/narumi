"""End-to-end server test: fake recorder → real pipeline job → minutes → transcript → export.

Runs the actual ``narumi.pipeline`` (ffmpeg + ``fake`` engines, ``none`` LLM) inside the server's
job worker; only the recorder is faked (``NARUMI_RECORDER=server/tests/fake_recorder.py``).
"""

from __future__ import annotations

import json
import threading
import uuid
from pathlib import Path

from click.testing import CliRunner
from conftest import PerCallClient, call, wait_job
from narumi.bundle import Bundle
from narumi_server import cli_tools
from narumi_server.context import ServerContext
from test_surface_tools import write_silence_wav  # shared helper (rootdir is on sys.path)

FAKE_CONFIG = {
    "transcription_engine": "fake",
    "diarization_engine": "fake",
    "llm_provider": "none",
}


def rid() -> str:
    return str(uuid.uuid4())


async def test_record_process_export_regenerate(client: PerCallClient, ctx: ServerContext):
    info = await call(client, "get_server_info")
    assert info["capabilities"]["recording"] is True
    assert "fake" in info["capabilities"]["transcription_engines"]
    assert "fake" in info["capabilities"]["diarization_engines"]
    assert {"none", "fake"} <= set(info["capabilities"]["llm_providers"])
    assert info["capabilities"]["export_destinations"] == [
        "markdown",
        "html",
        "notion",
        "gaia-library",
    ]

    # ---------------------------------------------------------------- record
    started = await call(
        client,
        "start_recording",
        {"meeting_name": "E2E サーバー定例", "config": FAKE_CONFIG, "request_id": rid()},
    )
    meeting_id = started["meeting_id"]
    assert set(started["tracks"]) == {"screen", "mic", "system"}

    stopped = await call(client, "stop_recording", {"request_id": rid(), "auto_process": True})
    assert stopped["meeting_id"] == meeting_id
    assert stopped["tracks"]["mic"]["sha256"] and stopped["tracks"]["system"]["sha256"]
    job_id = stopped["job_id"]

    # ---------------------------------------------------------------- process job
    job = await wait_job(ctx, job_id, timeout=120.0)
    assert job["status"] == "succeeded", job.get("error")
    status = await call(client, "get_job_status", {"job_id": job_id})
    assert status["job"]["status"] == "succeeded"
    assert status["job"]["kind"] == "process"
    result = status["job"]["result"]
    assert result["meeting_id"] == meeting_id and result["minutes_version"] == 1
    assert result["stages"][-1] == "minutes/v1" and result["skipped"] == []
    # The fake recorder's 1 s mic / system tracks overlap completely; the deterministic merge
    # keeps both speakers (me = mic, the system side refined to SPEAKER_00 by the fake diarizer),
    # neither resolved to a name (no self_name configured).
    assert result["unresolved_speakers"] == ["me", "SPEAKER_00"]

    # ---------------------------------------------------------------- read back
    meeting = await call(client, "get_meeting", {"meeting_id": meeting_id})
    assert meeting["meeting"]["status"] == "ready"
    assert meeting["latest_minutes"]["version"] == 1
    assert "## 文字起こし（全文）" in meeting["latest_minutes"]["markdown"]
    assert meeting["config"]["transcription_engine"] == "fake"
    assert "merged/merged" in meeting["artifacts"] and "minutes/v1" in meeting["artifacts"]
    assert meeting["minutes_versions"][0]["provider"] == "none"

    transcript = await call(client, "get_transcript", {"meeting_id": meeting_id})
    assert transcript["source"] == "merged" and transcript["segments"]
    assert transcript["segments"][0]["text"].startswith("ダミー発話")
    assert transcript["available_sources"] == ["merged", "own-mic", "own-system"]
    assert transcript["speaker_map"] == {
        "me": {"name": None, "confidence": 0.0},
        "SPEAKER_00": {"name": None, "confidence": 0.0},
    }
    assert [s["speaker"] for s in transcript["segments"]] == ["me", "SPEAKER_00"]
    assert [s["text"] for s in transcript["segments"]] == [
        transcript["segments"][0]["text"],
        transcript["segments"][1]["text"],
    ]  # both tracks' words survive the overlap
    own = await call(client, "get_transcript", {"meeting_id": meeting_id, "source": "own-mic"})
    assert [s["id"] for s in own["segments"]] == ["own-mic:0"]

    # ---------------------------------------------------------------- export (sync)
    exported = await call(
        client,
        "export_minutes",
        {"meeting_id": meeting_id, "destination": "markdown", "request_id": rid()},
    )
    assert exported["result"]["destination"] == "markdown"
    assert exported["result"]["minutes_version"] == 1
    assert exported["result"]["ref"].endswith(f"{meeting_id}-v1.md")
    assert [e["destination"] for e in ctx.catalog.list_exports(meeting_id)] == ["markdown"]

    listed = await call(client, "list_meetings", {})
    assert [m["meeting_id"] for m in listed["meetings"]] == [meeting_id]
    assert listed["meetings"][0]["latest_minutes_version"] == 1
    assert listed["meetings"][0]["status"] == "ready"
    by_text = await call(client, "list_meetings", {"query": "ダミー発話"})
    assert [m["meeting_id"] for m in by_text["meetings"]] == [meeting_id]

    # ---------------------------------------------------------------- regenerate (force) → v2
    regen = await call(
        client,
        "regenerate",
        {"meeting_id": meeting_id, "request_id": rid(), "force": True, "reason": "e2e"},
    )
    job = await wait_job(ctx, regen["job_id"], timeout=120.0)
    assert job["status"] == "succeeded", job.get("error")
    assert job["kind"] == "regenerate" and job["result"]["minutes_version"] == 2
    assert not any(key.startswith("preprocess/") for key in job["result"]["stages"])
    meeting = await call(client, "get_meeting", {"meeting_id": meeting_id})
    assert [v["version"] for v in meeting["minutes_versions"]] == [1, 2]
    assert meeting["latest_minutes"]["version"] == 2
    bundle = Bundle.find(ctx.meetings_root, meeting_id)
    assert [(r.job_id, r.reason) for r in bundle.manifest.regenerations] == [
        (regen["job_id"], "e2e")
    ]

    # ---------------------------------------------------------------- register a text context
    registered = await call(
        client,
        "register_context",
        {
            "meeting_id": meeting_id,
            "source_type": "text",
            "content": "参加者: 岡村、山田。前回の宿題: 資料更新。",
            "label": "メモ",
            "request_id": rid(),
        },
    )
    assert registered["status"] == "stored" and "job_id" not in registered
    meeting = await call(client, "get_meeting", {"meeting_id": meeting_id})
    assert [(c["context_id"], c["status"], c["label"]) for c in meeting["contexts"]] == [
        (registered["context_id"], "stored", "メモ")
    ]
    assert bundle.abspath(f"context/sources/{registered['context_id']}.json").is_file()

    # ------------------------------------------ register a VTT transcript (parsed) → regenerate
    vtt = "WEBVTT\n\n1\n00:00:00.200 --> 00:00:00.900\n山田: 議題を確認します。\n"
    parsed = await call(
        client,
        "register_context",
        {
            "meeting_id": meeting_id,
            "source_type": "zoom_transcript",
            "content": vtt,
            "label": "Zoom 字幕",
            "auto_regenerate": True,
            "request_id": rid(),
        },
    )
    assert parsed["status"] == "parsed"
    ext_source = f"ext-{parsed['context_id']}"
    job = await wait_job(ctx, parsed["job_id"], timeout=120.0)
    assert job["status"] == "succeeded", job.get("error")
    assert job["result"]["minutes_version"] == 3
    transcript = await call(client, "get_transcript", {"meeting_id": meeting_id})
    assert ext_source in transcript["available_sources"]
    # layer 4: the VTT's speaker name resolves the anonymous system-side label
    assert transcript["speaker_map"]["SPEAKER_00"]["name"] == "山田"


async def test_stop_without_auto_process_then_regenerate(client: PerCallClient, ctx: ServerContext):
    """The contract promises regenerate processes a meeting stopped with auto_process=false."""
    started = await call(
        client,
        "start_recording",
        {"meeting_name": "後で処理", "config": FAKE_CONFIG, "request_id": rid()},
    )
    meeting_id = started["meeting_id"]
    stopped = await call(client, "stop_recording", {"request_id": rid(), "auto_process": False})
    assert "job_id" not in stopped
    assert (await call(client, "get_meeting", {"meeting_id": meeting_id}))["meeting"]["status"] == (
        "recorded"
    )

    regen = await call(client, "regenerate", {"meeting_id": meeting_id, "request_id": rid()})
    job = await wait_job(ctx, regen["job_id"], timeout=120.0)
    assert job["status"] == "succeeded", job.get("error")
    assert job["kind"] == "regenerate" and job["result"]["minutes_version"] == 1
    assert job["result"]["stages"][0] == "preprocess/audio/mic"
    assert job["result"]["stages"][-1] == "minutes/v1"
    meeting = await call(client, "get_meeting", {"meeting_id": meeting_id})
    assert meeting["meeting"]["status"] == "ready"
    assert meeting["latest_minutes"]["version"] == 1
    assert "minutes/v1" in meeting["artifacts"] and "transcripts/own-mic" in meeting["artifacts"]
    bundle = Bundle.find(ctx.meetings_root, meeting_id)
    assert [r.job_id for r in bundle.manifest.regenerations] == [regen["job_id"]]

    # a config change through the API is honoured by the next regenerate: the diarizer is
    # switched off, its layer-2 artifact goes away and the minutes get a new version
    changed = await call(
        client,
        "set_meeting_config",
        {"meeting_id": meeting_id, "request_id": rid(), "diarization_engine": "none"},
    )
    assert changed["config"]["diarization_engine"] == "none"
    regen = await call(client, "regenerate", {"meeting_id": meeting_id, "request_id": rid()})
    job = await wait_job(ctx, regen["job_id"], timeout=120.0)
    assert job["status"] == "succeeded", job.get("error")
    assert job["result"]["stages"] == ["merged/merged", "minutes/v2"]
    assert "transcripts/own-mic" in job["result"]["skipped"]
    meeting = await call(client, "get_meeting", {"meeting_id": meeting_id})
    assert "diarization/layer2" not in meeting["artifacts"]
    transcript = await call(client, "get_transcript", {"meeting_id": meeting_id})
    assert [s["speaker"] for s in transcript["segments"]] == ["me", "other"]


async def test_policy_violation_is_rejected_at_config_time(
    client: PerCallClient, ctx: ServerContext
):
    """A provider the policy forbids is rejected at config time (never a silent fallback)."""
    denied = await call(
        client,
        "start_recording",
        {
            "meeting_name": "policy",
            "config": {**FAKE_CONFIG, "llm_provider": "anthropic-api"},
            "request_id": rid(),
        },
    )
    assert denied["error"]["code"] == "policy_violation"
    assert denied["error"]["details"]["provider"] == "anthropic-api"
    assert list(ctx.meetings_root.iterdir()) == []  # no orphan bundle


async def test_policy_violation_inside_the_job_fails_it(client: PerCallClient, ctx: ServerContext):
    """A violation that only surfaces inside the pipeline fails the job, never downgrades."""
    started = await call(
        client,
        "start_recording",
        {"meeting_name": "policy in job", "config": FAKE_CONFIG, "request_id": rid()},
    )
    meeting_id = started["meeting_id"]
    await call(client, "stop_recording", {"request_id": rid(), "auto_process": False})
    # bypass the handler's config check by editing the manifest on disk (someone edited the
    # bundle by hand): regenerate's fail-fast check catches it before enqueueing …
    bundle = Bundle.find(ctx.meetings_root, meeting_id)
    bundle.manifest.config.llm_provider = "anthropic-api"
    bundle.save()
    early = await call(client, "regenerate", {"meeting_id": meeting_id, "request_id": rid()})
    assert early["error"]["code"] == "policy_violation"
    # … and a violation introduced between enqueue and run is reported by the job itself: the
    # job re-reads the manifest once the worker becomes free. The acceptance itself now
    # holds the meeting lock, so occupy the job worker instead of blocking that lock.
    bundle.manifest.config.llm_provider = "none"
    bundle.save()
    release = threading.Event()

    def occupy_worker(_progress):
        assert release.wait(10)
        return {}

    blocker = ctx.jobs.submit("process", None, occupy_worker)
    try:
        regen = await call(client, "regenerate", {"meeting_id": meeting_id, "request_id": rid()})
        bundle.manifest.config.llm_provider = "anthropic-api"
        bundle.save()
    finally:
        release.set()
    assert (await wait_job(ctx, blocker))["status"] == "succeeded"
    job = await wait_job(ctx, regen["job_id"], timeout=120.0)
    assert job["status"] == "failed"
    assert job["error"]["code"] == "policy_violation"
    assert job["error"]["details"]["provider"] == "anthropic-api"
    meeting = await call(client, "get_meeting", {"meeting_id": meeting_id})
    assert meeting["meeting"]["status"] == "failed"


async def test_import_profile_export_lifecycle(
    client: PerCallClient, ctx: ServerContext, tmp_path: Path
):
    """Import → minutes → search → profile auto-export → discard → delete → rebuild, one flow.

    The whole surface-parity lifecycle in the order a user would drive it from the app: two
    imports through the real pipeline (fake engines), the second one picking up a saved default
    profile whose ``export_destinations`` trigger the auto-export.
    """
    mic = write_silence_wav(tmp_path / "mic.wav")
    system = write_silence_wav(tmp_path / "system.wav")

    # ------------------------------------------------------------ import #1 (explicit config)
    first = await call(
        client,
        "import_recording",
        {
            "meeting_name": "取り込み一本目",
            "mic_path": str(mic),
            "system_path": str(system),
            "started_at": "2026-08-27T09:00:00Z",
            "config": FAKE_CONFIG,
            "request_id": rid(),
        },
    )
    assert "error" not in first, first
    first_id = first["meeting_id"]
    job = await wait_job(ctx, first["job_id"], timeout=120.0)
    assert job["status"] == "succeeded", job.get("error")
    assert job["kind"] == "process"
    assert "exports" not in job["result"]  # no profile destinations configured yet

    minutes = await call(client, "get_minutes", {"meeting_id": first_id})
    assert minutes["version"] == 1 and minutes["available_versions"] == [1]
    assert minutes["markdown"]

    found = await call(client, "search_transcripts", {"query": "ダミー発話"})
    assert {h["meeting_id"] for h in found["hits"]} == {first_id}
    assert found["hits"][0]["meeting_name"] == "取り込み一本目"
    assert found["hits"][0]["segment_id"] and found["hits"][0]["source_id"]

    # ------------------------------------------------------------ profile with auto-export
    saved = await call(
        client,
        "set_profile",
        {
            "name": "auto-md",
            "config": FAKE_CONFIG,
            "export_destinations": ["markdown"],
            "make_default": True,
            "request_id": rid(),
        },
    )
    assert saved["profile"]["is_default"] is True
    assert (await call(client, "list_profiles"))["default"] == "auto-md"

    # ------------------------------------------------------------ import #2 (profile defaults)
    second = await call(
        client,
        "import_recording",
        {
            "meeting_name": "取り込み二本目",
            "mic_path": str(mic),
            "system_path": str(system),
            "started_at": "2026-08-27T10:00:00Z",
            "request_id": rid(),
        },
    )
    assert "error" not in second, second
    second_id = second["meeting_id"]
    job = await wait_job(ctx, second["job_id"], timeout=120.0)
    assert job["status"] == "succeeded", job.get("error")
    exports = job["result"]["exports"]  # the profile's destinations were auto-exported
    assert [e["destination"] for e in exports] == ["markdown"]
    assert exports[0]["minutes_version"] == 1
    assert Path(exports[0]["ref"]).is_file()
    assert "export_errors" not in job["result"]
    manifest = Bundle.find(ctx.meetings_root, second_id).manifest
    assert manifest.profile == "auto-md"
    assert manifest.config.transcription_engine == "fake"  # profile config applied as default
    assert [(e.destination, e.minutes_version) for e in manifest.exports] == [("markdown", 1)]

    # ------------------------------------------------------------ discard mic (transcript exists)
    meeting = await call(client, "get_meeting", {"meeting_id": second_id})
    assert "transcripts/own-mic" in meeting["artifacts"]
    discarded = await call(
        client,
        "discard_tracks",
        {"meeting_id": second_id, "tracks": ["mic"], "request_id": rid()},
    )
    assert discarded["tracks"]["mic"]["discarded"] is True
    assert len(discarded["tracks"]["mic"]["sha256"]) == 64  # sha256 survives the discard
    assert discarded["tracks"]["mic"]["bytes"] is None
    assert not (ctx.meetings_root / second_id / "tracks" / "mic.wav").exists()
    transcript = await call(client, "get_transcript", {"meeting_id": second_id})
    assert transcript["segments"]  # the derived transcript is untouched

    # ------------------------------------------------------------ delete meeting #1 → trash
    deleted = await call(
        client,
        "delete_meeting",
        {"meeting_id": first_id, "confirm": True, "request_id": rid()},
    )
    assert deleted["deleted"] is True
    moved_to = Path(deleted["moved_to"])
    assert moved_to.parent == ctx.data_root / "trash"
    assert (moved_to / "manifest.json").is_file()
    assert not (ctx.meetings_root / first_id).exists()

    # ------------------------------------------------------------ rebuild counts the survivor
    rebuilt = await call(client, "rebuild_catalog", {"request_id": rid()})
    assert rebuilt["meetings"] == 1
    assert rebuilt["segments"] >= 1
    assert rebuilt["errors"] == []
    listed = await call(client, "list_meetings")
    assert [m["meeting_id"] for m in listed["meetings"]] == [second_id]
    found = await call(client, "search_transcripts", {"query": "ダミー発話"})
    assert {h["meeting_id"] for h in found["hits"]} == {second_id}


def test_product_cli_import_then_list_meetings_in_process(home: Path, tmp_path: Path):
    """``narumi`` (product CLI) drives import + list in-process — no server, no subprocess."""
    cli = cli_tools.build_cli()
    runner = CliRunner()
    mic = write_silence_wav(tmp_path / "mic.wav")

    imported = runner.invoke(
        cli,
        [
            "--in-process",
            "import-recording",
            "--meeting-name",
            "CLI 取り込み",
            "--mic-path",
            str(mic),
            "--no-auto-process",
        ],
        catch_exceptions=False,
    )
    assert imported.exit_code == 0, imported.stderr
    meeting_id = json.loads(imported.stdout)["meeting_id"]

    listed = runner.invoke(cli, ["--in-process", "list-meetings"], catch_exceptions=False)
    assert listed.exit_code == 0, listed.stderr
    payload = json.loads(listed.stdout)
    assert [m["meeting_id"] for m in payload["meetings"]] == [meeting_id]
    assert payload["meetings"][0]["status"] == "recorded"
    assert payload["meetings"][0]["active_job"] is None
