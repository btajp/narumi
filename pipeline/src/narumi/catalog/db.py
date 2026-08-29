"""``narumi.db``: a rebuildable catalog + full-text index over session bundles.

AGENTS.md 絶対原則 1: the bundle on disk is the source of truth; this database is a derived index.
Every row in ``meetings`` / ``exports`` / ``contexts`` / ``segments_fts`` can be regenerated from
``meetings/<id>/manifest.json`` and ``merged/merged.json`` (see :meth:`Catalog.rebuild`). Only
``jobs`` (volatile), ``requests`` (idempotency replay cache) and ``audit_log`` (append-only server
history) hold state that does not live in a bundle.

Scope semantics (default deny / explicit allow) are implemented here so that the server and the
dev CLI cannot disagree:

* requested scope ``None`` → only meetings whose ``scope IS NULL``
* one name → that scope **or** unscoped meetings
* two or more names → those scopes or unscoped, and an ``audit_log`` row ``cross_scope_read``
"""

from __future__ import annotations

import json
import re
import secrets
import sqlite3
import threading
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from narumi.bundle import Bundle, utc_now_iso
from narumi.catalog.rebuild import RebuildStats
from narumi.errors import (
    ConfigurationConflictError,
    InvalidArgumentError,
    NotFoundError,
    ScopeDeniedError,
)
from narumi.models import MergedTranscript

MERGED_ARTIFACT_KEY = "merged/merged"
MERGED_DEFAULT_PATH = "merged/merged.json"
MERGED_SOURCE_ID = "merged"
JOB_STATUSES: tuple[str, ...] = ("queued", "running", "succeeded", "failed", "cancelled")
ACTIVE_JOB_STATUSES: tuple[str, ...] = ("queued", "running")
CROSS_SCOPE_ACTION = "cross_scope_read"
TRIGRAM_MIN_CHARS = 3
"""FTS5 ``trigram`` needs at least three characters; shorter queries fall back to ``LIKE``."""
PROCESSING_RUN_ID_RE = re.compile(r"^run-[0-9a-f]{32}$")

MEETING_COLUMNS: tuple[str, ...] = (
    "meeting_id",
    "meeting_name",
    "engagement",
    "scope",
    "status",
    "started_at",
    "stopped_at",
    "bundle_path",
    "latest_minutes_version",
    "updated_at",
)
SUMMARY_COLUMNS: tuple[str, ...] = (
    "meeting_id",
    "meeting_name",
    "engagement",
    "scope",
    "status",
    "started_at",
    "stopped_at",
    "latest_minutes_version",
)

_DERIVED_TABLES: tuple[str, ...] = ("meetings", "exports", "contexts", "segments_fts")

_SCHEMA: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS meetings (
        meeting_id TEXT PRIMARY KEY,
        meeting_name TEXT NOT NULL,
        engagement TEXT,
        scope TEXT,
        status TEXT NOT NULL,
        started_at TEXT NOT NULL,
        stopped_at TEXT,
        bundle_path TEXT NOT NULL,
        latest_minutes_version INTEGER,
        updated_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS meetings_started_at ON meetings(started_at DESC)",
    "CREATE INDEX IF NOT EXISTS meetings_scope ON meetings(scope)",
    """
    CREATE TABLE IF NOT EXISTS jobs (
        job_id TEXT PRIMARY KEY,
        meeting_id TEXT,
        kind TEXT NOT NULL,
        status TEXT NOT NULL,
        processing_run_id TEXT,
        progress TEXT,
        result_json TEXT,
        error_json TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS jobs_meeting ON jobs(meeting_id, status)",
    """
    CREATE TABLE IF NOT EXISTS exports (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        meeting_id TEXT NOT NULL,
        destination TEXT NOT NULL,
        ref TEXT NOT NULL,
        minutes_version INTEGER NOT NULL,
        at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS exports_meeting ON exports(meeting_id)",
    """
    CREATE TABLE IF NOT EXISTS contexts (
        context_id TEXT PRIMARY KEY,
        meeting_id TEXT NOT NULL,
        source_type TEXT NOT NULL,
        status TEXT NOT NULL,
        registered_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS contexts_meeting ON contexts(meeting_id)",
    """
    CREATE TABLE IF NOT EXISTS audit_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        actor TEXT NOT NULL,
        action TEXT NOT NULL,
        detail_json TEXT NOT NULL,
        at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS requests (
        request_id TEXT PRIMARY KEY,
        tool TEXT NOT NULL,
        meeting_id TEXT,
        response_json TEXT NOT NULL,
        at TEXT NOT NULL
    )
    """,
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS segments_fts USING fts5(
        meeting_id UNINDEXED,
        source_id UNINDEXED,
        segment_id UNINDEXED,
        start_sec UNINDEXED,
        end_sec UNINDEXED,
        speaker UNINDEXED,
        text,
        tokenize='trigram'
    )
    """,
)


def normalize_scope(requested: str | Sequence[str] | None) -> list[str]:
    """Turn the contract's ``scope`` selector into a de-duplicated list of names.

    ``None`` → ``[]`` (unscoped only). Empty names / empty lists / wrong types raise
    :class:`InvalidArgumentError` — a silent "allow everything" must never happen.
    """
    if requested is None:
        return []
    if isinstance(requested, str):
        if not requested.strip():
            raise InvalidArgumentError("scope name must not be empty")
        return [requested]
    if isinstance(requested, Sequence):
        names: list[str] = []
        for item in requested:
            if not isinstance(item, str) or not item.strip():
                raise InvalidArgumentError(
                    "scope list entries must be non-empty strings",
                    details={"scope": list(requested)},
                )
            if item not in names:
                names.append(item)
        if not names:
            raise InvalidArgumentError("scope list must not be empty")
        return names
    raise InvalidArgumentError(
        f"scope must be a string or a list of strings, got {type(requested).__name__}"
    )


def row_to_summary(row: Mapping[str, Any]) -> dict[str, Any]:
    """Project a ``meetings`` row onto the contract's ``meeting_summary`` shape.

    ``active_job`` (attached by :meth:`Catalog.list_meetings`) is carried over when present;
    rows from :meth:`Catalog.get_meeting_row` do not have it and the key is then omitted.
    """
    summary = {column: row.get(column) for column in SUMMARY_COLUMNS}
    if "active_job" in row:
        summary["active_job"] = row["active_job"]
    return summary


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _loads(text: str | None) -> Any:
    return None if text is None else json.loads(text)


def _like_pattern(query: str) -> str:
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _fts_phrase(query: str) -> str:
    """Quote ``query`` as one FTS5 phrase so operators / punctuation are literal."""
    return '"' + query.replace('"', '""') + '"'


class Catalog:
    """SQLite-backed index; safe to share between threads (one connection + re-entrant lock)."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        if str(self.db_path) != ":memory:":
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(self.db_path), check_same_thread=False, isolation_level=None, timeout=30.0
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=OFF")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._create_schema()

    # ------------------------------------------------------------------ lifecycle
    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> Catalog:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

    def _create_schema(self) -> None:
        with self._tx() as conn:
            for statement in _SCHEMA:
                conn.execute(statement)
            job_columns = {
                str(row["name"]) for row in conn.execute("PRAGMA table_info(jobs)").fetchall()
            }
            if "processing_run_id" not in job_columns:
                conn.execute("ALTER TABLE jobs ADD COLUMN processing_run_id TEXT")

    # ------------------------------------------------------------------ meetings
    def upsert_meeting(self, bundle: Bundle) -> None:
        """Refresh every derived row of one meeting from its manifest (exports / contexts too)."""
        manifest = bundle.manifest
        recording = manifest.recording
        with self._tx() as conn:
            conn.execute(
                """
                INSERT INTO meetings (meeting_id, meeting_name, engagement, scope, status,
                    started_at, stopped_at, bundle_path, latest_minutes_version, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(meeting_id) DO UPDATE SET
                    meeting_name = excluded.meeting_name,
                    engagement = excluded.engagement,
                    scope = excluded.scope,
                    status = excluded.status,
                    started_at = excluded.started_at,
                    stopped_at = excluded.stopped_at,
                    bundle_path = excluded.bundle_path,
                    latest_minutes_version = excluded.latest_minutes_version,
                    updated_at = excluded.updated_at
                """,
                (
                    manifest.meeting_id,
                    manifest.meeting_name,
                    manifest.engagement,
                    manifest.scope,
                    manifest.status,
                    recording.started_at or manifest.created_at,
                    recording.stopped_at,
                    str(bundle.path),
                    manifest.latest_minutes_version,
                    utc_now_iso(),
                ),
            )
            conn.execute("DELETE FROM exports WHERE meeting_id = ?", (manifest.meeting_id,))
            conn.executemany(
                "INSERT INTO exports (meeting_id, destination, ref, minutes_version, at)"
                " VALUES (?, ?, ?, ?, ?)",
                [
                    (manifest.meeting_id, e.destination, e.ref, e.minutes_version, e.at)
                    for e in manifest.exports
                ],
            )
            conn.execute("DELETE FROM contexts WHERE meeting_id = ?", (manifest.meeting_id,))
            conn.executemany(
                "INSERT OR REPLACE INTO contexts"
                " (context_id, meeting_id, source_type, status, registered_at)"
                " VALUES (?, ?, ?, ?, ?)",
                [
                    (c.context_id, manifest.meeting_id, c.source_type, c.status, c.registered_at)
                    for c in manifest.contexts
                ],
            )

    def get_meeting_row(self, meeting_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM meetings WHERE meeting_id = ?", (meeting_id,)
            ).fetchone()
        return None if row is None else dict(row)

    def delete_meeting(self, meeting_id: str) -> None:
        """Drop every derived row of one meeting (used when a bundle disappears)."""
        with self._tx() as conn:
            for table in ("meetings", "exports", "contexts", "segments_fts"):
                conn.execute(f"DELETE FROM {table} WHERE meeting_id = ?", (meeting_id,))  # noqa: S608

    def list_meetings(
        self,
        *,
        query: str | None = None,
        since: str | None = None,
        until: str | None = None,
        scope: str | Sequence[str] | None = None,
        limit: int = 50,
        actor: str = "server",
    ) -> list[dict[str, Any]]:
        """Newest first. ``since`` (inclusive) / ``until`` (exclusive) compare ``started_at``."""
        if limit < 1:
            raise InvalidArgumentError("limit must be >= 1", details={"limit": limit})
        if since is not None and until is not None and since >= until:
            raise InvalidArgumentError(
                "range.from must be earlier than range.to", details={"from": since, "to": until}
            )
        scopes = normalize_scope(scope)
        where, params = self._scope_clause("scope", scopes)
        if since is not None:
            where.append("started_at >= ?")
            params.append(since)
        if until is not None:
            where.append("started_at < ?")
            params.append(until)
        if query:
            if len(query) >= TRIGRAM_MIN_CHARS:
                segment_sql = "SELECT meeting_id FROM segments_fts WHERE segments_fts MATCH ?"
                segment_param = _fts_phrase(query)
            else:
                segment_sql = "SELECT meeting_id FROM segments_fts WHERE text LIKE ? ESCAPE '\\'"
                segment_param = _like_pattern(query)
            where.append(
                "(meeting_name LIKE ? ESCAPE '\\' OR engagement LIKE ? ESCAPE '\\'"
                f" OR meeting_id IN ({segment_sql}))"
            )
            params.extend([_like_pattern(query), _like_pattern(query), segment_param])
        sql = (
            "SELECT * FROM meetings WHERE "
            + " AND ".join(where)
            + " ORDER BY started_at DESC, meeting_id DESC LIMIT ?"
        )
        params.append(limit)
        with self._lock:
            self._audit_cross_scope(actor, scopes, action="list_meetings", meeting_id=None)
            rows = self._conn.execute(sql, params).fetchall()
            meetings = [dict(row) for row in rows]
            self._attach_active_jobs(meetings)
        return meetings

    def _attach_active_jobs(self, meetings: list[dict[str, Any]]) -> None:
        """Set ``active_job`` on every row: the newest queued / running job (``None`` if idle).

        Caller holds ``self._lock``.
        """
        for row in meetings:
            row["active_job"] = None
        ids = [row["meeting_id"] for row in meetings]
        if not ids:
            return
        placeholders = ", ".join("?" for _ in ids)
        job_rows = self._conn.execute(
            "SELECT job_id, meeting_id, kind, status, progress FROM jobs"
            f" WHERE status IN ('queued', 'running') AND meeting_id IN ({placeholders})"  # noqa: S608
            " ORDER BY created_at DESC, rowid DESC",  # rowid: insertion order within one second
            ids,
        ).fetchall()
        newest: dict[str, sqlite3.Row] = {}
        for job in job_rows:
            newest.setdefault(job["meeting_id"], job)
        for row in meetings:
            job = newest.get(row["meeting_id"])
            if job is None:
                continue
            active: dict[str, Any] = {
                "job_id": job["job_id"],
                "kind": job["kind"],
                "status": job["status"],
            }
            progress = _loads(job["progress"])
            if progress is not None:
                active["progress"] = progress
            row["active_job"] = active

    def check_scope(
        self,
        meeting_scope: str | None,
        requested: str | Sequence[str] | None,
        *,
        actor: str = "server",
        meeting_id: str | None = None,
    ) -> None:
        """Raise :class:`ScopeDeniedError` unless ``requested`` covers ``meeting_scope``.

        Unscoped meetings are always readable; a scoped meeting needs its scope in the request.
        A request naming two or more scopes is audit-logged as ``cross_scope_read``.
        """
        scopes = normalize_scope(requested)
        with self._lock:
            self._audit_cross_scope(actor, scopes, action="check_scope", meeting_id=meeting_id)
        if meeting_scope is None or meeting_scope in scopes:
            return
        raise ScopeDeniedError(
            f"meeting belongs to scope {meeting_scope!r}, which the request does not cover",
            details={
                "meeting_id": meeting_id,
                "meeting_scope": meeting_scope,
                "requested_scope": scopes or None,
            },
        )

    @staticmethod
    def _scope_clause(column: str, scopes: list[str]) -> tuple[list[str], list[Any]]:
        if not scopes:
            return [f"{column} IS NULL"], []
        placeholders = ", ".join("?" for _ in scopes)
        return [f"({column} IS NULL OR {column} IN ({placeholders}))"], list(scopes)

    def _audit_cross_scope(
        self, actor: str, scopes: list[str], *, action: str, meeting_id: str | None
    ) -> None:
        if len(scopes) < 2:
            return
        detail: dict[str, Any] = {"scopes": scopes, "action": action}
        if meeting_id is not None:
            detail["meeting_id"] = meeting_id
        self.audit(actor, CROSS_SCOPE_ACTION, detail)

    # ------------------------------------------------------------------ segments (FTS)
    def index_segments(self, bundle: Bundle) -> int:
        """(Re)index ``merged/merged.json`` of one bundle. Returns the number of rows written."""
        merged = self._load_merged(bundle)
        rows = []
        if merged is not None:
            for seg in merged.segments:
                rows.append(
                    (
                        bundle.meeting_id,
                        MERGED_SOURCE_ID,
                        seg.id,
                        seg.start,
                        seg.end,
                        seg.speaker_name or seg.speaker_label,
                        seg.text,
                    )
                )
        with self._tx() as conn:
            conn.execute("DELETE FROM segments_fts WHERE meeting_id = ?", (bundle.meeting_id,))
            conn.executemany(
                "INSERT INTO segments_fts"
                " (meeting_id, source_id, segment_id, start_sec, end_sec, speaker, text)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
        return len(rows)

    @staticmethod
    def _load_merged(bundle: Bundle) -> MergedTranscript | None:
        record = bundle.artifact(MERGED_ARTIFACT_KEY)
        rel = record.path if record is not None else MERGED_DEFAULT_PATH
        path = bundle.abspath(rel)
        if not path.is_file():
            return None
        return MergedTranscript.model_validate_json(path.read_text(encoding="utf-8"))

    def search_segments(
        self,
        text: str,
        *,
        scope: str | Sequence[str] | None = None,
        limit: int = 50,
        actor: str = "server",
    ) -> list[dict[str, Any]]:
        """Full-text search over indexed segments, restricted by the same scope rules.

        Hit shape matches the ``search_transcripts`` contract: ``meeting_id``, ``meeting_name``,
        ``source_id``, ``segment_id``, ``start``, ``end``, ``speaker`` (name or label, ``None``
        when unknown) and ``text`` — ordered newest meeting first, best match first within a
        meeting.
        """
        if not text:
            raise InvalidArgumentError("search text must not be empty")
        if limit < 1:
            raise InvalidArgumentError("limit must be >= 1", details={"limit": limit})
        scopes = normalize_scope(scope)
        where, params = self._scope_clause("m.scope", scopes)
        if len(text) >= TRIGRAM_MIN_CHARS:
            where.append("segments_fts MATCH ?")
            params.append(_fts_phrase(text))
            order = "ORDER BY m.started_at DESC, m.meeting_id DESC, rank, s.start_sec"
        else:
            where.append("s.text LIKE ? ESCAPE '\\'")
            params.append(_like_pattern(text))
            order = "ORDER BY m.started_at DESC, m.meeting_id DESC, s.start_sec"
        sql = (
            "SELECT s.meeting_id, m.meeting_name, s.source_id, s.segment_id, s.start_sec,"
            " s.end_sec, s.speaker, s.text"
            " FROM segments_fts s JOIN meetings m ON m.meeting_id = s.meeting_id"
            " WHERE " + " AND ".join(where) + f" {order} LIMIT ?"
        )
        params.append(limit)
        with self._lock:
            self._audit_cross_scope(actor, scopes, action="search_segments", meeting_id=None)
            rows = self._conn.execute(sql, params).fetchall()
        return [
            {
                "meeting_id": row["meeting_id"],
                "meeting_name": row["meeting_name"],
                "source_id": row["source_id"],
                "segment_id": row["segment_id"],
                "start": row["start_sec"],
                "end": row["end_sec"],
                "speaker": row["speaker"],
                "text": row["text"],
            }
            for row in rows
        ]

    # ------------------------------------------------------------------ rebuild
    def rebuild(self, meetings_root: Path, *, actor: str = "catalog") -> RebuildStats:
        """Drop the derived tables and re-create them from every bundle under ``meetings_root``.

        ``jobs``, ``requests`` and ``audit_log`` are kept: none of them is derivable from bundles
        (the audit log in particular must survive a rebuild). Broken bundles are reported in
        ``errors`` and skipped; they never abort the rebuild of the others.
        """
        root = Path(meetings_root)
        stats = RebuildStats()
        with self._tx() as conn:
            for table in _DERIVED_TABLES:
                conn.execute(f"DROP TABLE IF EXISTS {table}")  # noqa: S608
            for statement in _SCHEMA:
                conn.execute(statement)
        if root.is_dir():
            for child in sorted(root.iterdir()):
                if not child.is_dir() or not (child / "manifest.json").exists():
                    continue
                try:
                    bundle = Bundle.open(child)
                    self.upsert_meeting(bundle)
                    stats.segments += self.index_segments(bundle)
                    stats.meetings += 1
                except Exception as exc:  # noqa: BLE001 - one broken bundle must not hide the rest
                    stats.errors.append(f"{child.name}: {type(exc).__name__}: {exc}")
        self.audit(
            actor,
            "catalog_rebuild",
            {
                "meetings_root": str(root),
                "meetings": stats.meetings,
                "segments": stats.segments,
                "errors": list(stats.errors),
            },
        )
        return stats

    # ------------------------------------------------------------------ jobs
    def create_job(self, kind: str, meeting_id: str | None) -> str:
        if not kind:
            raise InvalidArgumentError("job kind must not be empty")
        job_id = f"job-{secrets.token_hex(8)}"
        now = utc_now_iso()
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO jobs (job_id, meeting_id, kind, status, created_at, updated_at)"
                " VALUES (?, ?, ?, 'queued', ?, ?)",
                (job_id, meeting_id, kind, now, now),
            )
        return job_id

    def update_job(
        self,
        job_id: str,
        *,
        status: str | None = None,
        progress: dict[str, Any] | None = None,
        result: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
    ) -> None:
        if status is not None and status not in JOB_STATUSES:
            raise InvalidArgumentError(f"unknown job status {status!r}", details={"status": status})
        sets: list[str] = ["updated_at = ?"]
        params: list[Any] = [utc_now_iso()]
        if status is not None:
            sets.append("status = ?")
            params.append(status)
        if progress is not None:
            sets.append("progress = ?")
            params.append(_dumps(progress))
        if result is not None:
            sets.append("result_json = ?")
            params.append(_dumps(result))
        if error is not None:
            sets.append("error_json = ?")
            params.append(_dumps(error))
        params.append(job_id)
        with self._tx() as conn:
            cursor = conn.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE job_id = ?", params)  # noqa: S608
            if cursor.rowcount == 0:
                raise NotFoundError(f"job not found: {job_id}", details={"job_id": job_id})

    def attach_job_processing_run(self, job_id: str, processing_run_id: str) -> None:
        """Correlate one job with its durable ensemble run exactly once."""
        if (
            not isinstance(processing_run_id, str)
            or PROCESSING_RUN_ID_RE.fullmatch(processing_run_id) is None
        ):
            raise InvalidArgumentError(
                "invalid processing run ID", details={"processing_run_id": processing_run_id}
            )
        with self._tx() as conn:
            row = conn.execute(
                "SELECT kind, status, processing_run_id FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"job not found: {job_id}", details={"job_id": job_id})
            if row["kind"] not in ("process", "regenerate"):
                raise InvalidArgumentError(
                    "only processing jobs can reference an ensemble run",
                    details={"job_id": job_id, "kind": row["kind"]},
                )
            current = row["processing_run_id"]
            if current is not None and current != processing_run_id:
                raise ConfigurationConflictError(
                    "job already references another processing run",
                    details={
                        "job_id": job_id,
                        "processing_run_id": current,
                    },
                )
            if current is None:
                if row["status"] not in ACTIVE_JOB_STATUSES:
                    raise ConfigurationConflictError(
                        "finished job cannot be attached to a processing run",
                        details={"job_id": job_id, "status": row["status"]},
                    )
                conn.execute(
                    "UPDATE jobs SET processing_run_id = ?, updated_at = ? WHERE job_id = ?",
                    (processing_run_id, utc_now_iso(), job_id),
                )

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        return None if row is None else self._job_from_row(row)

    def list_jobs(
        self,
        *,
        meeting_id: str | None = None,
        statuses: Sequence[str] | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        where: list[str] = ["1 = 1"]
        params: list[Any] = []
        if meeting_id is not None:
            where.append("meeting_id = ?")
            params.append(meeting_id)
        if statuses:
            where.append(f"status IN ({', '.join('?' for _ in statuses)})")
            params.extend(statuses)
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM jobs WHERE "  # noqa: S608
                + " AND ".join(where)
                + " ORDER BY created_at DESC, job_id DESC LIMIT ?",
                params,
            ).fetchall()
        return [self._job_from_row(row) for row in rows]

    def mark_stale_jobs(self, message: str) -> int:
        """Fail every ``queued`` / ``running`` job (call once at server start-up)."""
        error = {"code": "internal", "message": message}
        with self._tx() as conn:
            cursor = conn.execute(
                "UPDATE jobs SET status = 'failed', error_json = ?, updated_at = ?"
                " WHERE status IN ('queued', 'running')",
                (_dumps(error), utc_now_iso()),
            )
        return cursor.rowcount

    @staticmethod
    def _job_from_row(row: sqlite3.Row) -> dict[str, Any]:
        job: dict[str, Any] = {"job_id": row["job_id"]}
        if row["meeting_id"] is not None:
            job["meeting_id"] = row["meeting_id"]
        job["kind"] = row["kind"]
        job["status"] = row["status"]
        job["processing_run_id"] = row["processing_run_id"]
        for key, column in (
            ("progress", "progress"),
            ("result", "result_json"),
            ("error", "error_json"),
        ):
            value = _loads(row[column])
            if value is not None:
                if key == "result" and isinstance(value, dict):
                    value.setdefault("processing_run_id", job["processing_run_id"])
                job[key] = value
        job["created_at"] = row["created_at"]
        job["updated_at"] = row["updated_at"]
        return job

    # ------------------------------------------------------------------ exports / contexts
    def record_export(
        self, meeting_id: str, destination: str, ref: str, minutes_version: int, at: str
    ) -> None:
        with self._tx() as conn:
            exists = conn.execute(
                "SELECT 1 FROM exports WHERE meeting_id = ? AND destination = ? AND ref = ?"
                " AND minutes_version = ? AND at = ?",
                (meeting_id, destination, ref, minutes_version, at),
            ).fetchone()
            if exists is None:
                conn.execute(
                    "INSERT INTO exports (meeting_id, destination, ref, minutes_version, at)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (meeting_id, destination, ref, minutes_version, at),
                )

    def list_exports(self, meeting_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT destination, ref, minutes_version, at FROM exports"
                " WHERE meeting_id = ? ORDER BY id",
                (meeting_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def record_context(
        self, meeting_id: str, context_id: str, source_type: str, status: str, registered_at: str
    ) -> None:
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO contexts (context_id, meeting_id, source_type, status, registered_at)"
                " VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT(context_id) DO UPDATE SET meeting_id = excluded.meeting_id,"
                " source_type = excluded.source_type, status = excluded.status,"
                " registered_at = excluded.registered_at",
                (context_id, meeting_id, source_type, status, registered_at),
            )

    def list_contexts(self, meeting_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT context_id, meeting_id, source_type, status, registered_at FROM contexts"
                " WHERE meeting_id = ? ORDER BY registered_at, rowid",
                (meeting_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------ idempotency
    def get_request(self, request_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM requests WHERE request_id = ?", (request_id,)
            ).fetchone()
        if row is None:
            return None
        return {
            "request_id": row["request_id"],
            "tool": row["tool"],
            "meeting_id": row["meeting_id"],
            "response": _loads(row["response_json"]),
            "at": row["at"],
        }

    def save_request(
        self, request_id: str, tool: str, meeting_id: str | None, response: Mapping[str, Any]
    ) -> None:
        if not request_id:
            raise InvalidArgumentError("request_id must not be empty")
        with self._tx() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO requests (request_id, tool, meeting_id, response_json, at)"
                " VALUES (?, ?, ?, ?, ?)",
                (request_id, tool, meeting_id, _dumps(dict(response)), utc_now_iso()),
            )

    # ------------------------------------------------------------------ audit
    def audit(self, actor: str, action: str, detail: Mapping[str, Any]) -> None:
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO audit_log (actor, action, detail_json, at) VALUES (?, ?, ?, ?)",
                (actor, action, _dumps(dict(detail)), utc_now_iso()),
            )

    def list_audit(self, limit: int = 100, *, action: str | None = None) -> list[dict[str, Any]]:
        where = "WHERE action = ?" if action is not None else ""
        params: list[Any] = [action] if action is not None else []
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM audit_log {where} ORDER BY id DESC LIMIT ?",  # noqa: S608
                params,
            ).fetchall()
        return [
            {
                "id": row["id"],
                "actor": row["actor"],
                "action": row["action"],
                "detail": _loads(row["detail_json"]),
                "at": row["at"],
            }
            for row in rows
        ]
