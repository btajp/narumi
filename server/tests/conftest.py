"""Shared fixtures: an isolated data root, the fake recorder and an in-memory MCP client."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import anyio
import pytest
from mcp.client import Client
from mcp.server import Server
from mcp_types import CallToolResult, ListToolsResult
from narumi.bundle import Bundle, MinutesVersionRecord, utc_now_iso
from narumi.bundle.manifest_writer import LOCK_DIRECTORY_NAME
from narumi.models import MergedSegment, MergedTranscript, MinutesMeta, SpeakerEntry, SpeakerMap
from narumi.pipeline import ProcessResult
from narumi_server.app import build_server
from narumi_server.context import ServerContext, build_context

FAKE_RECORDER = Path(__file__).resolve().parent / "fake_recorder.py"
TRANSPORT = "in-memory"


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "home"
    monkeypatch.setenv("NARUMI_HOME", str(root))
    monkeypatch.setenv("NARUMI_RECORDER", str(FAKE_RECORDER))
    for knob in (
        "FAKE_RECORDER_FAIL",
        "FAKE_RECORDER_START_DELAY",
        "FAKE_RECORDER_STOP_DELAY",
        "FAKE_RECORDER_CRASH_ON_STOP",
        "FAKE_RECORDER_ERROR_AFTER_STOP",
        "FAKE_RECORDER_CHECK",
        "FAKE_RECORDER_PERMISSION_DELAY",
        "FAKE_RECORDER_PERMISSION_RESULT",
        "FAKE_RECORDER_PERMISSION_EXIT",
        "FAKE_RECORDER_PERMISSION_IGNORE_TERM",
        "FAKE_RECORDER_PERMISSION_MARKER",
        "NARUMI_CONTRACTS_DIR",
        "NARUMI_GAIA_URL",
        "NARUMI_GAIA_API_KEY",
    ):
        monkeypatch.delenv(knob, raising=False)
    return root


@pytest.fixture
def ctx(home: Path) -> Iterator[ServerContext]:
    context = build_context(home, transports=[TRANSPORT], validate_output=True)
    yield context
    context.close()


@pytest.fixture
def server(ctx: ServerContext) -> Server[Any]:
    return build_server(ctx)


class PerCallClient:
    """``mcp.client.Client`` opened in-process for every call.

    pytest-asyncio runs an async-generator fixture's setup and teardown in different tasks, which
    anyio cancel scopes (inside ``Client``) reject; a session per call avoids holding one open
    across the fixture boundary. ``test_single_session_flow`` covers the persistent-session path.
    """

    def __init__(self, server: Server[Any]) -> None:
        self.server = server

    @asynccontextmanager
    async def session(self) -> AsyncIterator[Client]:
        async with Client(self.server) as mcp_client:
            yield mcp_client

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> CallToolResult:
        async with self.session() as mcp_client:
            return await mcp_client.call_tool(name, arguments)

    async def list_tools(self) -> ListToolsResult:
        async with self.session() as mcp_client:
            return await mcp_client.list_tools()


@pytest.fixture
def client(server: Server[Any]) -> PerCallClient:
    return PerCallClient(server)


async def call(
    client: PerCallClient | Client, name: str, args: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Call a tool and return its structured content (result or error envelope)."""
    result = await client.call_tool(name, args or {})
    assert isinstance(result.structured_content, dict)
    if result.is_error:
        assert "error" in result.structured_content
    else:
        assert "error" not in result.structured_content
    return result.structured_content


async def wait_job(ctx: ServerContext, job_id: str, timeout: float = 20.0) -> dict[str, Any]:
    return await anyio.to_thread.run_sync(ctx.jobs.wait, job_id, timeout)


def started_at_from_id(meeting_id: str) -> str:
    """``20260827T010000Z-…`` → ``2026-08-27T01:00:00Z`` (the id embeds the UTC start)."""
    stamp = meeting_id.split("-", 1)[0]
    return f"{stamp[0:4]}-{stamp[4:6]}-{stamp[6:8]}T{stamp[9:11]}:{stamp[11:13]}:{stamp[13:15]}Z"


def meeting_entries(ctx: ServerContext) -> list[Path]:
    """Return real meeting-root entries, excluding only the durable lock directory."""
    return sorted(
        (
            entry
            for entry in ctx.meetings_root.iterdir()
            if not (entry.name == LOCK_DIRECTORY_NAME and entry.is_dir())
        ),
        key=lambda entry: entry.name,
    )


def make_recorded_bundle(
    ctx: ServerContext,
    *,
    meeting_id: str,
    name: str = "テスト会議",
    scope: str | None = None,
    engagement: str | None = None,
) -> Bundle:
    bundle = Bundle.create(
        ctx.meetings_root,
        meeting_name=name,
        meeting_id=meeting_id,
        scope=scope,
        engagement=engagement,
    )
    started = started_at_from_id(meeting_id)
    bundle.manifest.recording.started_at = started
    bundle.manifest.recording.stopped_at = started
    bundle.manifest.recording.duration_sec = 1.0
    bundle.manifest.status = "recorded"
    bundle.save()
    ctx.catalog.upsert_meeting(bundle)
    return bundle


def write_fake_minutes(bundle: Bundle, text: str = "# 議事録\n\n- 決定事項 A\n") -> int:
    """Append a minutes version + merged transcript the way a pipeline run would."""
    merged = MergedTranscript(
        segments=[
            MergedSegment(
                id="merged:0",
                start=0.0,
                end=1.5,
                text="オンボーディング資料を来週までに更新する",
                speaker_label="me",
                speaker_name="岡村",
                sources=["own-mic:0"],
            ),
            MergedSegment(
                id="merged:1",
                start=1.6,
                end=3.0,
                text="了解しました",
                speaker_label="other",
                speaker_name=None,
                sources=["own-system:0"],
            ),
        ],
        speaker_map=SpeakerMap(
            speakers={
                "me": SpeakerEntry(name="岡村", confidence=1.0),
                "other": SpeakerEntry(name=None, confidence=0.0),
            }
        ),
    )
    bundle.run_stage(
        "merged/merged",
        inputs={"transcripts/own-mic": "deadbeef"},
        params={"fake": True},
        producer=("fake", "1"),
        output="merged/merged.json",
        fn=lambda out: out.write_text(merged.model_dump_json(indent=2), encoding="utf-8"),
        force=True,
    )
    version = bundle.next_minutes_version()
    minutes_dir = bundle.minutes_dir(version)
    (minutes_dir / "minutes.md").write_text(text, encoding="utf-8")
    bundle.write_json(
        f"minutes/v{version}/meta.json",
        MinutesMeta(
            version=version,
            generated_at=utc_now_iso(),
            provider="none",
            unresolved_speakers=["other"],
        ),
    )
    bundle.run_stage(
        f"minutes/v{version}",
        inputs={"merged/merged": bundle.artifact_hash("merged/merged")},
        params={"provider": "none"},
        producer=("fake-minutes", "1"),
        output=f"minutes/v{version}/minutes.md",
        fn=lambda out: None,
    )
    bundle.manifest.minutes_versions.append(
        MinutesVersionRecord(
            version=version,
            path=f"minutes/v{version}/minutes.md",
            generated_at=utc_now_iso(),
            provider="none",
        )
    )
    bundle.save()
    return version


def fake_process_meeting(
    bundle: Bundle, *, force: bool = False, progress=None, gaia_client_factory=None
) -> ProcessResult:
    """Stand-in for ``narumi.pipeline.process_meeting`` used via monkeypatch."""
    if progress is not None:
        progress("transcribe", 0.4)
    version = write_fake_minutes(bundle)
    if progress is not None:
        progress("generate", 1.0)
    return ProcessResult(
        meeting_id=bundle.meeting_id,
        minutes_version=version,
        stages=["merged/merged", f"minutes/v{version}"],
        skipped=[],
        unresolved_speakers=["other"],
    )
