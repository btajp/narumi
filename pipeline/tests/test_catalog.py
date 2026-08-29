"""Tests for ``narumi.catalog`` (rebuildable index, scope rules, FTS, jobs, requests, audit)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from narumi.bundle import Bundle, ContextRecord, ExportRecord, MinutesVersionRecord
from narumi.catalog import (
    CROSS_SCOPE_ACTION,
    Catalog,
    RebuildStats,
    normalize_scope,
    rebuild_catalog,
    row_to_summary,
)
from narumi.errors import (
    ConfigurationConflictError,
    InvalidArgumentError,
    NotFoundError,
    ScopeDeniedError,
)
from narumi.models import MergedSegment, MergedTranscript, SpeakerEntry, SpeakerMap


# ---------------------------------------------------------------------------- helpers
def make_bundle(
    meetings_root: Path,
    *,
    meeting_id: str,
    name: str = "会議",
    scope: str | None = None,
    engagement: str | None = None,
    status: str = "recorded",
    started_at: str = "2026-08-27T03:05:00Z",
) -> Bundle:
    bundle = Bundle.create(
        meetings_root, meeting_name=name, meeting_id=meeting_id, scope=scope, engagement=engagement
    )
    bundle.manifest.status = status  # type: ignore[assignment]
    bundle.manifest.recording.started_at = started_at
    bundle.save()
    return bundle


def write_merged(bundle: Bundle, texts: list[str], *, force: bool = False) -> None:
    merged = MergedTranscript(
        segments=[
            MergedSegment(
                id=f"merged:{i}",
                start=float(i),
                end=float(i) + 0.9,
                text=text,
                speaker_label="me" if i % 2 == 0 else "other",
                speaker_name="岡村" if i % 2 == 0 else None,
            )
            for i, text in enumerate(texts)
        ],
        speaker_map=SpeakerMap(speakers={"me": SpeakerEntry(name="岡村", confidence=1.0)}),
    )
    bundle.run_stage(
        "merged/merged",
        inputs={"transcripts/own-mic": "abc"},
        params={},
        producer=("fake", "1"),
        output="merged/merged.json",
        fn=lambda out: out.write_text(merged.model_dump_json(), encoding="utf-8"),
        force=force,
    )


@pytest.fixture
def meetings(tmp_path: Path) -> Path:
    root = tmp_path / "meetings"
    root.mkdir()
    return root


@pytest.fixture
def catalog(tmp_path: Path):
    cat = Catalog(tmp_path / "narumi.db")
    yield cat
    cat.close()


# ---------------------------------------------------------------------------- basics
def test_schema_created_and_reopenable(tmp_path: Path):
    db = tmp_path / "sub" / "narumi.db"
    Catalog(db).close()
    assert db.exists()
    with Catalog(db) as cat:  # idempotent CREATE IF NOT EXISTS
        assert cat.list_meetings() == []


def test_upsert_and_get_meeting_row(catalog: Catalog, meetings: Path):
    bundle = make_bundle(
        meetings, meeting_id="20260827T030500Z-a1b2c3d4", name="週次定例", scope="cloudnative"
    )
    bundle.manifest.minutes_versions.append(
        MinutesVersionRecord(
            version=2,
            path="minutes/v2/minutes.md",
            generated_at="2026-08-27T04:00:00Z",
            provider="none",
        )
    )
    bundle.manifest.exports.append(
        ExportRecord(
            destination="markdown", ref="/tmp/x.md", minutes_version=2, at="2026-08-27T04:10:00Z"
        )
    )
    bundle.manifest.contexts.append(
        ContextRecord(
            context_id="ctx-0123abcd",
            source_type="text",
            registered_at="2026-08-27T04:05:00Z",
            path="context/sources/ctx-0123abcd.json",
        )
    )
    bundle.save()
    catalog.upsert_meeting(bundle)

    row = catalog.get_meeting_row(bundle.meeting_id)
    assert row is not None
    assert row["meeting_name"] == "週次定例"
    assert row["scope"] == "cloudnative"
    assert row["status"] == "recorded"
    assert row["started_at"] == "2026-08-27T03:05:00Z"
    assert row["latest_minutes_version"] == 2
    assert row["bundle_path"] == str(bundle.path)
    assert row_to_summary(row) == {
        "meeting_id": bundle.meeting_id,
        "meeting_name": "週次定例",
        "engagement": None,
        "scope": "cloudnative",
        "status": "recorded",
        "started_at": "2026-08-27T03:05:00Z",
        "stopped_at": None,
        "latest_minutes_version": 2,
    }
    assert catalog.list_exports(bundle.meeting_id) == [
        {
            "destination": "markdown",
            "ref": "/tmp/x.md",
            "minutes_version": 2,
            "at": "2026-08-27T04:10:00Z",
        }
    ]
    assert [c["context_id"] for c in catalog.list_contexts(bundle.meeting_id)] == ["ctx-0123abcd"]

    # upsert again → no duplicated exports / contexts, updated status
    bundle.manifest.status = "ready"
    bundle.save()
    catalog.upsert_meeting(bundle)
    assert catalog.get_meeting_row(bundle.meeting_id)["status"] == "ready"  # type: ignore[index]
    assert len(catalog.list_exports(bundle.meeting_id)) == 1
    assert catalog.get_meeting_row("20260101T000000Z-00000000") is None


def test_started_at_falls_back_to_created_at(catalog: Catalog, meetings: Path):
    bundle = Bundle.create(meetings, meeting_name="x", meeting_id="20260827T030500Z-ffffffff")
    catalog.upsert_meeting(bundle)
    row = catalog.get_meeting_row(bundle.meeting_id)
    assert row is not None and row["started_at"] == bundle.manifest.created_at


# ---------------------------------------------------------------------------- scope rules
@pytest.fixture
def scoped(catalog: Catalog, meetings: Path) -> dict[str, Bundle]:
    bundles = {
        "unscoped": make_bundle(
            meetings, meeting_id="20260827T010000Z-00000001", started_at="2026-08-27T01:00:00Z"
        ),
        "cn": make_bundle(
            meetings,
            meeting_id="20260827T020000Z-00000002",
            scope="cloudnative",
            started_at="2026-08-27T02:00:00Z",
        ),
        "bt": make_bundle(
            meetings,
            meeting_id="20260827T030000Z-00000003",
            scope="btcon",
            started_at="2026-08-27T03:00:00Z",
        ),
    }
    for b in bundles.values():
        catalog.upsert_meeting(b)
    return bundles


def ids(rows: list[dict]) -> list[str]:
    return [r["meeting_id"] for r in rows]


def test_list_meetings_scope_default_deny(catalog: Catalog, scoped: dict[str, Bundle]):
    assert ids(catalog.list_meetings()) == [scoped["unscoped"].meeting_id]
    assert catalog.list_audit(action=CROSS_SCOPE_ACTION) == []


def test_list_meetings_single_scope_includes_unscoped(catalog: Catalog, scoped: dict[str, Bundle]):
    rows = catalog.list_meetings(scope="cloudnative")
    assert ids(rows) == [scoped["cn"].meeting_id, scoped["unscoped"].meeting_id]  # newest first
    assert catalog.list_audit(action=CROSS_SCOPE_ACTION) == []


def test_list_meetings_cross_scope_is_audited(catalog: Catalog, scoped: dict[str, Bundle]):
    rows = catalog.list_meetings(scope=["cloudnative", "btcon"], actor="agent:x")
    assert ids(rows) == [
        scoped["bt"].meeting_id,
        scoped["cn"].meeting_id,
        scoped["unscoped"].meeting_id,
    ]
    audit = catalog.list_audit(action=CROSS_SCOPE_ACTION)
    assert len(audit) == 1
    assert audit[0]["actor"] == "agent:x"
    assert audit[0]["detail"]["scopes"] == ["cloudnative", "btcon"]
    assert audit[0]["detail"]["action"] == "list_meetings"
    # a single-element list behaves like a single name and is not audited
    assert ids(catalog.list_meetings(scope=["btcon"])) == [
        scoped["bt"].meeting_id,
        scoped["unscoped"].meeting_id,
    ]
    assert len(catalog.list_audit(action=CROSS_SCOPE_ACTION)) == 1


def test_list_meetings_range_and_limit(catalog: Catalog, scoped: dict[str, Bundle]):
    rows = catalog.list_meetings(
        scope=["cloudnative", "btcon"], since="2026-08-27T02:00:00Z", until="2026-08-27T03:00:00Z"
    )
    assert ids(rows) == [scoped["cn"].meeting_id]
    assert len(catalog.list_meetings(scope=["cloudnative", "btcon"], limit=2)) == 2
    with pytest.raises(InvalidArgumentError):
        catalog.list_meetings(since="2026-08-27T03:00:00Z", until="2026-08-27T02:00:00Z")
    with pytest.raises(InvalidArgumentError):
        catalog.list_meetings(limit=0)


def test_normalize_scope():
    assert normalize_scope(None) == []
    assert normalize_scope("a") == ["a"]
    assert normalize_scope(["a", "b", "a"]) == ["a", "b"]
    for bad in ("", [], [""], 5, ["a", 1]):
        with pytest.raises(InvalidArgumentError):
            normalize_scope(bad)  # type: ignore[arg-type]


def test_check_scope(catalog: Catalog):
    catalog.check_scope(None, None)
    catalog.check_scope(None, "cloudnative")
    catalog.check_scope("cloudnative", "cloudnative")
    catalog.check_scope("cloudnative", ["btcon", "cloudnative"], meeting_id="m1", actor="agent")
    with pytest.raises(ScopeDeniedError) as exc:
        catalog.check_scope("cloudnative", None, meeting_id="20260827T020000Z-00000002")
    assert exc.value.details["meeting_scope"] == "cloudnative"
    assert exc.value.details["requested_scope"] is None
    with pytest.raises(ScopeDeniedError):
        catalog.check_scope("cloudnative", "btcon")
    with pytest.raises(ScopeDeniedError):
        catalog.check_scope("cloudnative", ["btcon", "acme"])
    audit = catalog.list_audit(action=CROSS_SCOPE_ACTION)
    assert [a["detail"].get("meeting_id") for a in audit] == [None, "m1"]
    assert audit[1]["detail"]["scopes"] == ["btcon", "cloudnative"]
    assert audit[1]["actor"] == "agent"


# ---------------------------------------------------------------------------- FTS
def test_index_and_search_segments(catalog: Catalog, meetings: Path):
    cn = make_bundle(meetings, meeting_id="20260827T020000Z-00000002", scope="cloudnative")
    write_merged(cn, ["では定例を始めます。", "オンボーディング資料を来週までに更新する"])
    plain = make_bundle(meetings, meeting_id="20260827T010000Z-00000001", name="社内 kickoff")
    write_merged(plain, ["kickoff meeting agenda"])
    empty = make_bundle(meetings, meeting_id="20260827T000000Z-00000000")
    for b in (cn, plain, empty):
        catalog.upsert_meeting(b)
        catalog.index_segments(b)
    assert catalog.index_segments(empty) == 0

    hits = catalog.search_segments("オンボーディング", scope="cloudnative")
    assert [h["segment_id"] for h in hits] == ["merged:1"]
    assert hits[0]["meeting_id"] == cn.meeting_id
    assert hits[0]["speaker"] == "other"
    assert hits[0]["start"] == 1.0 and hits[0]["end"] == 1.9
    # default deny: the scoped meeting's segments are invisible without its scope
    assert catalog.search_segments("オンボーディング") == []
    # short queries (< 3 chars) use LIKE instead of the trigram index
    assert [h["text"] for h in catalog.search_segments("定例", scope="cloudnative")] == [
        "では定例を始めます。"
    ]
    assert catalog.search_segments("ki")[0]["meeting_id"] == plain.meeting_id
    # quotes / operators in the query are literal, never FTS syntax
    assert catalog.search_segments('agenda" OR "x') == []
    with pytest.raises(InvalidArgumentError):
        catalog.search_segments("")

    # list_meetings(query=...) matches name, engagement or transcript text
    assert ids(catalog.list_meetings(query="資料を来週", scope="cloudnative")) == [cn.meeting_id]
    assert ids(catalog.list_meetings(query="資料を来週")) == []  # scoped meeting stays hidden
    assert ids(catalog.list_meetings(query="kickoff")) == [plain.meeting_id]
    assert ids(catalog.list_meetings(query="agenda")) == [plain.meeting_id]
    assert ids(catalog.list_meetings(query="nothing-here")) == []
    assert ids(catalog.list_meetings(query="50%")) == []

    # re-index replaces rows instead of appending (force: same inputs would be skipped)
    write_merged(cn, ["別のテキスト"], force=True)
    catalog.index_segments(cn)
    assert catalog.search_segments("オンボーディング", scope="cloudnative") == []
    assert len(catalog.search_segments("別のテキスト", scope="cloudnative")) == 1


# ---------------------------------------------------------------------------- rebuild
def test_rebuild_from_bundles(tmp_path: Path, meetings: Path):
    a = make_bundle(meetings, meeting_id="20260827T010000Z-00000001", name="A")
    write_merged(a, ["one two three", "four five six"])
    make_bundle(meetings, meeting_id="20260827T020000Z-00000002", name="B", scope="cloudnative")
    broken = meetings / "20260827T030000Z-00000003"
    broken.mkdir()
    (broken / "manifest.json").write_text("{not json", encoding="utf-8")
    (meetings / "stray.txt").write_text("ignored", encoding="utf-8")

    db = tmp_path / "narumi.db"
    with Catalog(db) as cat:
        # stale rows that must disappear after the rebuild
        ghost = make_bundle(tmp_path / "elsewhere", meeting_id="20260827T040000Z-00000004")
        cat.upsert_meeting(ghost)
        job_id = cat.create_job("process", a.meeting_id)
        cat.save_request("req-1", "regenerate", a.meeting_id, {"job_id": job_id})
        cat.audit("server", "something", {"x": 1})

        stats = cat.rebuild(meetings)
        assert isinstance(stats, RebuildStats)
        assert stats.meetings == 2
        assert stats.segments == 2
        assert len(stats.errors) == 1 and stats.errors[0].startswith("20260827T030000Z-00000003:")

        assert ids(cat.list_meetings(scope="cloudnative")) == [
            "20260827T020000Z-00000002",
            "20260827T010000Z-00000001",
        ]
        assert cat.get_meeting_row(ghost.meeting_id) is None
        assert ids(cat.list_meetings(query="five six")) == [a.meeting_id]
        # volatile / append-only tables survive
        assert cat.get_job(job_id) is not None
        assert cat.get_request("req-1") is not None
        actions = [row["action"] for row in cat.list_audit()]
        assert "something" in actions and "catalog_rebuild" in actions

    # rebuild_catalog convenience wrapper opens and closes the DB itself
    stats2 = rebuild_catalog(db, meetings)
    assert stats2.meetings == 2


def test_rebuild_missing_root_is_empty(tmp_path: Path):
    with Catalog(tmp_path / "narumi.db") as cat:
        stats = cat.rebuild(tmp_path / "nope")
    assert stats == RebuildStats(meetings=0, segments=0, errors=[])


# ---------------------------------------------------------------------------- jobs
def test_jobs_lifecycle(catalog: Catalog):
    job_id = catalog.create_job("process", "20260827T010000Z-00000001")
    assert job_id.startswith("job-") and len(job_id) == 4 + 16
    job = catalog.get_job(job_id)
    assert job is not None
    assert job["status"] == "queued" and job["kind"] == "process"
    assert job["meeting_id"] == "20260827T010000Z-00000001"
    assert set(job) == {
        "job_id",
        "meeting_id",
        "kind",
        "status",
        "processing_run_id",
        "created_at",
        "updated_at",
    }
    assert job["processing_run_id"] is None

    catalog.update_job(job_id, status="running", progress={"stage": "transcribe", "fraction": 0.4})
    job = catalog.get_job(job_id)
    assert job is not None and job["status"] == "running"
    assert job["progress"] == {"stage": "transcribe", "fraction": 0.4}

    run_id = "run-" + "1" * 32
    catalog.attach_job_processing_run(job_id, run_id)
    catalog.attach_job_processing_run(job_id, run_id)  # idempotent
    catalog.update_job(job_id, status="succeeded", result={"minutes_version": 1})
    job = catalog.get_job(job_id)
    assert job is not None and job["processing_run_id"] == run_id
    assert job["result"] == {"minutes_version": 1, "processing_run_id": run_id}

    with pytest.raises(ConfigurationConflictError):
        catalog.attach_job_processing_run(job_id, "run-" + "2" * 32)
    with pytest.raises(InvalidArgumentError):
        catalog.attach_job_processing_run(job_id, "bad-run")

    failed = catalog.create_job("export", None)
    catalog.update_job(failed, status="failed", error={"code": "policy_violation", "message": "no"})
    job = catalog.get_job(failed)
    assert job is not None and "meeting_id" not in job
    assert job["processing_run_id"] is None
    assert job["error"] == {"code": "policy_violation", "message": "no"}
    with pytest.raises(InvalidArgumentError):
        catalog.attach_job_processing_run(failed, run_id)

    legacy_done = catalog.create_job("process", None)
    catalog.update_job(legacy_done, status="succeeded", result={})
    with pytest.raises(ConfigurationConflictError):
        catalog.attach_job_processing_run(legacy_done, run_id)

    assert [j["job_id"] for j in catalog.list_jobs(statuses=["failed"])] == [failed]
    assert catalog.get_job("job-000000000000") is None
    with pytest.raises(NotFoundError):
        catalog.update_job("job-000000000000", status="running")
    with pytest.raises(InvalidArgumentError):
        catalog.update_job(job_id, status="bogus")
    with pytest.raises(InvalidArgumentError):
        catalog.create_job("", None)


def test_existing_job_table_is_migrated_with_nullable_processing_run_id(tmp_path: Path):
    db = tmp_path / "narumi.db"
    connection = sqlite3.connect(db)
    connection.execute(
        """
        CREATE TABLE jobs (
            job_id TEXT PRIMARY KEY,
            meeting_id TEXT,
            kind TEXT NOT NULL,
            status TEXT NOT NULL,
            progress TEXT,
            result_json TEXT,
            error_json TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        "INSERT INTO jobs (job_id, kind, status, created_at, updated_at)"
        " VALUES ('job-1111111111111111', 'process', 'queued', ?, ?)",
        ("2026-08-29T00:00:00Z", "2026-08-29T00:00:00Z"),
    )
    connection.commit()
    connection.close()

    with Catalog(db) as catalog:
        job = catalog.get_job("job-1111111111111111")
        assert job is not None and job["processing_run_id"] is None
        columns = {
            row[1]
            for row in catalog._conn.execute("PRAGMA table_info(jobs)").fetchall()  # noqa: SLF001
        }
        assert "processing_run_id" in columns


def test_mark_stale_jobs(catalog: Catalog):
    a = catalog.create_job("process", None)
    b = catalog.create_job("regenerate", None)
    catalog.update_job(b, status="running")
    done = catalog.create_job("export", None)
    catalog.update_job(done, status="succeeded", result={})
    assert catalog.mark_stale_jobs("server restarted") == 2
    for job_id in (a, b):
        job = catalog.get_job(job_id)
        assert job is not None and job["status"] == "failed"
        assert job["error"]["code"] == "internal"
    assert catalog.get_job(done)["status"] == "succeeded"  # type: ignore[index]


# ---------------------------------------------------------------------------- requests / audit
def test_requests_roundtrip(catalog: Catalog):
    assert catalog.get_request("missing") is None
    catalog.save_request("req-1", "start_recording", None, {"meeting_id": "m", "tracks": {}})
    stored = catalog.get_request("req-1")
    assert stored is not None
    assert stored["tool"] == "start_recording"
    assert stored["response"] == {"meeting_id": "m", "tracks": {}}
    assert stored["meeting_id"] is None
    catalog.save_request("req-1", "start_recording", "m", {"meeting_id": "m"})
    assert catalog.get_request("req-1")["meeting_id"] == "m"  # type: ignore[index]
    with pytest.raises(InvalidArgumentError):
        catalog.save_request("", "x", None, {})


def test_record_export_and_context(catalog: Catalog):
    catalog.record_export("m1", "markdown", "/tmp/a.md", 1, "2026-08-27T04:15:00Z")
    catalog.record_export("m1", "markdown", "/tmp/a.md", 1, "2026-08-27T04:15:00Z")  # dedup
    catalog.record_export("m1", "html", "/tmp/a.html", 1, "2026-08-27T04:16:00Z")
    assert [e["destination"] for e in catalog.list_exports("m1")] == ["markdown", "html"]
    catalog.record_context("m1", "ctx-0123abcd", "text", "stored", "2026-08-27T04:05:00Z")
    catalog.record_context("m1", "ctx-0123abcd", "text", "parsed", "2026-08-27T04:05:00Z")
    rows = catalog.list_contexts("m1")
    assert len(rows) == 1 and rows[0]["status"] == "parsed"


def test_audit_log(catalog: Catalog):
    catalog.audit("server", "start_recording", {"meeting_id": "m1", "path": Path("/x")})
    catalog.audit("agent", "export", {"n": 2})
    rows = catalog.list_audit(limit=10)
    assert [r["action"] for r in rows] == ["export", "start_recording"]  # newest first
    assert rows[1]["detail"] == {"meeting_id": "m1", "path": "/x"}
    assert catalog.list_audit(limit=1)[0]["actor"] == "agent"
    assert json.dumps(rows) is not None  # plain JSON data


# ---------------------------------------------------------------------------- active_job
def test_list_meetings_attaches_active_job(catalog: Catalog, meetings: Path):
    a = make_bundle(meetings, meeting_id="20260827T010000Z-00000001", name="A")
    b = make_bundle(
        meetings,
        meeting_id="20260827T020000Z-00000002",
        name="B",
        started_at="2026-08-27T02:00:00Z",
    )
    for bundle in (a, b):
        catalog.upsert_meeting(bundle)

    rows = catalog.list_meetings()
    assert [row["active_job"] for row in rows] == [None, None]

    job_id = catalog.create_job("process", a.meeting_id)
    catalog.update_job(job_id, status="running", progress={"stage": "transcribe", "fraction": 0.4})
    done = catalog.create_job("export", b.meeting_id)
    catalog.update_job(done, status="succeeded", result={})

    by_id = {row["meeting_id"]: row for row in catalog.list_meetings()}
    assert by_id[a.meeting_id]["active_job"] == {
        "job_id": job_id,
        "kind": "process",
        "status": "running",
        "progress": {"stage": "transcribe", "fraction": 0.4},
    }
    assert by_id[b.meeting_id]["active_job"] is None  # finished jobs are not active

    # the newest queued / running job wins when several are active
    newer = catalog.create_job("regenerate", a.meeting_id)
    active = {r["meeting_id"]: r["active_job"] for r in catalog.list_meetings()}[a.meeting_id]
    assert active is not None and active["job_id"] == newer
    assert active["kind"] == "regenerate" and active["status"] == "queued"
    assert "progress" not in active

    # row_to_summary carries active_job through when present, and omits it otherwise
    summary = row_to_summary(by_id[a.meeting_id])
    assert summary["active_job"]["job_id"] == job_id
    row = catalog.get_meeting_row(a.meeting_id)
    assert row is not None and "active_job" not in row_to_summary(row)

    catalog.update_job(job_id, status="cancelled", error={"code": "cancelled", "message": "x"})
    catalog.update_job(newer, status="failed", error={"code": "internal", "message": "x"})
    assert [r["active_job"] for r in catalog.list_meetings()] == [None, None]


# ---------------------------------------------------------------------------- search hit shape
def test_search_segments_hit_shape_and_order(catalog: Catalog, meetings: Path):
    old = make_bundle(
        meetings,
        meeting_id="20260820T030500Z-00c0ffee",
        name="先週定例",
        started_at="2026-08-20T03:05:00Z",
    )
    write_merged(old, ["オンボーディング資料は gaia-library に置きます。"])
    new = make_bundle(
        meetings,
        meeting_id="20260827T030500Z-a1b2c3d4",
        name="週次定例",
        started_at="2026-08-27T03:05:00Z",
    )
    write_merged(new, ["先週のオンボーディングの進捗から共有します。"])
    for bundle in (old, new):
        catalog.upsert_meeting(bundle)
        catalog.index_segments(bundle)

    hits = catalog.search_segments("オンボーディング")
    # newest meeting first; every hit carries the full search_transcripts contract shape
    assert [h["meeting_id"] for h in hits] == [new.meeting_id, old.meeting_id]
    assert [h["meeting_name"] for h in hits] == ["週次定例", "先週定例"]
    assert hits[0] == {
        "meeting_id": new.meeting_id,
        "meeting_name": "週次定例",
        "source_id": "merged",
        "segment_id": "merged:0",
        "start": 0.0,
        "end": 0.9,
        "speaker": "岡村",
        "text": "先週のオンボーディングの進捗から共有します。",
    }
    assert catalog.search_segments("オンボーディング", limit=1) == [hits[0]]
    # the short-query LIKE fallback orders the same way
    short = catalog.search_segments("進捗")
    assert [h["meeting_id"] for h in short] == [new.meeting_id]
