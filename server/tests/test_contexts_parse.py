"""register_context parsing: transcript payloads become transcripts/ext-<context_id> artifacts."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

from conftest import PerCallClient, call, make_recorded_bundle
from narumi.bundle import Bundle
from narumi.bundle.hashing import sha256_file
from narumi.models import Transcript
from narumi_server.context import ServerContext

MEETING = "20260827T014500Z-00000c0a"

VTT = """WEBVTT

1
00:00:03.190 --> 00:00:06.850
岡村 慎太郎: では定例を始めます。よろしくお願いします。

2
00:00:07.200 --> 00:00:11.480
田中 太郎: 先週のリリース状況について共有します。
"""

ZOOM_TXT = "00:00:03 岡村 慎太郎: では定例を始めます。\n00:00:07 田中 太郎: 進捗を共有します。\n"


def rid() -> str:
    return str(uuid.uuid4())


async def register(client: PerCallClient, **extra) -> dict:
    args = {"meeting_id": MEETING, "request_id": rid(), **extra}
    return await call(client, "register_context", args)


async def test_vtt_content_is_parsed_into_ext_transcript(client: PerCallClient, ctx: ServerContext):
    bundle = make_recorded_bundle(ctx, meeting_id=MEETING)
    result = await register(client, source_type="zoom_transcript", content=VTT, label="Zoom 字幕")
    assert result["status"] == "parsed"
    context_id = result["context_id"]

    bundle = Bundle.find(ctx.meetings_root, MEETING)
    record = bundle.manifest.artifacts[f"transcripts/ext-{context_id}"]
    assert record.path == f"transcripts/ext-{context_id}.json"
    assert record.params == {"parser": "vtt", "version": 1}
    assert record.producer.name == "parser-vtt"
    source_path = bundle.path / "context" / "sources" / f"{context_id}.json"
    assert record.inputs == {f"context/{context_id}": sha256_file(source_path)}

    transcript = Transcript.model_validate_json(
        (bundle.path / record.path).read_text(encoding="utf-8")
    )
    assert transcript.source_id == f"ext-{context_id}" and transcript.kind == "external"
    assert [s.speaker for s in transcript.segments] == ["岡村 慎太郎", "田中 太郎"]
    assert transcript.segments[0].start == 3.19

    assert bundle.manifest.contexts[-1].status == "parsed"
    rows = ctx.catalog.list_contexts(MEETING)
    assert rows[-1]["status"] == "parsed"

    # the parsed source is served by get_transcript
    served = await call(
        client, "get_transcript", {"meeting_id": MEETING, "source": f"ext-{context_id}"}
    )
    assert [s["speaker"] for s in served["segments"]] == ["岡村 慎太郎", "田中 太郎"]


async def test_zoom_txt_and_notion_minutes_parse(client: PerCallClient, ctx: ServerContext):
    make_recorded_bundle(ctx, meeting_id=MEETING)
    zoom = await register(client, source_type="zoom_transcript", content=ZOOM_TXT)
    assert zoom["status"] == "parsed"
    bundle = Bundle.find(ctx.meetings_root, MEETING)
    zoom_record = bundle.manifest.artifacts[f"transcripts/ext-{zoom['context_id']}"]
    assert zoom_record.params["parser"] == "zoom_txt"

    notion = await register(
        client, source_type="notion_ai_minutes", content="岡村: では定例を始めます。"
    )
    assert notion["status"] == "parsed"
    bundle = Bundle.find(ctx.meetings_root, MEETING)
    notion_record = bundle.manifest.artifacts[f"transcripts/ext-{notion['context_id']}"]
    assert notion_record.params["parser"] == "plain"
    transcript = Transcript.model_validate_json(
        (bundle.path / notion_record.path).read_text(encoding="utf-8")
    )
    assert transcript.segments[0].speaker is None  # plain treatment: no speaker extraction
    assert transcript.engine.params["confidence"] == "low"


async def test_url_stays_stored_and_is_never_fetched(client: PerCallClient, ctx: ServerContext):
    make_recorded_bundle(ctx, meeting_id=MEETING)
    result = await register(client, source_type="url", url="https://example.com/minutes")
    assert result["status"] == "stored"
    bundle = Bundle.find(ctx.meetings_root, MEETING)
    assert bundle.manifest.contexts[-1].status == "stored"
    stored = json.loads(
        (bundle.path / "context" / "sources" / f"{result['context_id']}.json").read_text(
            encoding="utf-8"
        )
    )
    assert stored["url"] == "https://example.com/minutes"
    assert "content" not in stored
    assert not any(key.startswith("transcripts/ext-") for key in bundle.manifest.artifacts)


async def test_non_transcript_source_types_stay_stored(client: PerCallClient, ctx: ServerContext):
    make_recorded_bundle(ctx, meeting_id=MEETING)
    # a VTT payload under a non-transcript source_type is context, not a transcript
    doc = await register(client, source_type="document", content=VTT)
    assert doc["status"] == "stored"
    chat = await register(client, source_type="chat_log", content="岡村: 了解です")
    assert chat["status"] == "stored"
    bundle = Bundle.find(ctx.meetings_root, MEETING)
    assert [c.status for c in bundle.manifest.contexts] == ["stored", "stored"]
    assert not any(key.startswith("transcripts/ext-") for key in bundle.manifest.artifacts)


async def test_binary_file_payload_stays_stored(
    client: PerCallClient, ctx: ServerContext, tmp_path: Path
):
    make_recorded_bundle(ctx, meeting_id=MEETING)
    blob = tmp_path / "capture.bin"
    blob.write_bytes(b"\x00\x01\x02\xff not text")
    result = await register(client, source_type="zoom_transcript", file_path=str(blob))
    assert result["status"] == "stored"
    bundle = Bundle.find(ctx.meetings_root, MEETING)
    stored = json.loads(
        (bundle.path / "context" / "sources" / f"{result['context_id']}.json").read_text(
            encoding="utf-8"
        )
    )
    assert stored["content_encoding"] == "base64"
    assert not any(key.startswith("transcripts/ext-") for key in bundle.manifest.artifacts)


async def test_text_file_payload_is_parsed_from_stored_copy(
    client: PerCallClient, ctx: ServerContext, tmp_path: Path
):
    make_recorded_bundle(ctx, meeting_id=MEETING)
    exported = tmp_path / "meeting.vtt"
    exported.write_text(VTT, encoding="utf-8")
    result = await register(client, source_type="teams_transcript", file_path=str(exported))
    assert result["status"] == "parsed"
    bundle = Bundle.find(ctx.meetings_root, MEETING)
    record = bundle.manifest.artifacts[f"transcripts/ext-{result['context_id']}"]
    assert record.params["parser"] == "vtt"
