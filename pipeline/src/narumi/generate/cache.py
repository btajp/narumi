"""Interval-level result cache for stage 2 (Step 8: affected-interval re-run).

``merged/integrate_cache.json`` maps an interval *fingerprint* — a sha256 over everything that
can change the interval's merged rows (contributing segment ids + texts, provider,
prompt_version, the speaker turns overlapping the interval, and the other prompt inputs) — to
the merged segment rows that interval produced. On re-run, intervals whose fingerprint is
unchanged reuse their cached rows without an LLM call; only intervals actually touched by a new
or changed source are recomputed（対応表に 1 列追加 → 影響区間だけ第 2 段を再実行）.

The cache is derived data, not an artifact: it is not recorded in the manifest, and deleting it
only costs LLM calls on the next integrate run. ``save`` keeps only the fingerprints the current
run touched, so the file always mirrors the latest ``merged/merged.json``.

Cache file format (version 1)::

    {
      "version": 1,
      "provider": "<provider name>",
      "prompt_version": "integrate-v1",
      "entries": {
        "<fingerprint sha256>": [
          {"start": 12.0, "end": 16.0, "text": "…", "speaker_label": "me",
           "sources": ["own-mic:1"]},
          …
        ]
      }
    }

Rows deliberately omit ``id`` (merged ids are renumbered globally on assembly) and
``speaker_name`` (resolved from the fresh speaker map / layer-4 names on every run, so a changed
``self_name`` or speaker resolution never serves a stale name).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from narumi.bundle.hashing import canonical_json, sha256_bytes
from narumi.generate.speakers import overlap
from narumi.models import Interval, Segment, Turn

CACHE_PATH = "merged/integrate_cache.json"
CACHE_VERSION = 1
FINGERPRINT_VERSION = 1
ROW_KEYS = ("start", "end", "text", "speaker_label", "sources")
"""Keys of one cached merged row (a :class:`~narumi.models.MergedSegment` minus id / name)."""


def interval_fingerprint(
    interval: Interval,
    contributing: list[str],
    index: dict[str, tuple[str, Segment]],
    turns: list[Turn],
    *,
    provider: str,
    prompt_version: str,
    reference: str,
    language: str,
    vocab_hints: list[str],
) -> str:
    """Content hash of everything that determines this interval's merged rows.

    ``turns`` are the aligned-clock speaker turns of every layer; only those overlapping the
    interval enter the hash, so a new layer-4 name far away never invalidates this interval.
    """
    columns = sorted(
        [seg_id, index[seg_id][1].text]
        for source_id in contributing
        for seg_id in interval.columns[source_id]
    )
    speakers = sorted(
        [round(turn.start, 3), round(turn.end, 3), turn.speaker, turn.layer, turn.source_id or ""]
        for turn in turns
        if overlap(interval.start, interval.end, turn.start, turn.end) > 0
    )
    payload = {
        "v": FINGERPRINT_VERSION,
        "provider": provider,
        "prompt_version": prompt_version,
        "reference": reference,
        "language": language,
        "vocab_hints": list(vocab_hints),
        "span": [round(interval.start, 3), round(interval.end, 3)],
        "columns": columns,
        "speakers": speakers,
    }
    return sha256_bytes(canonical_json(payload).encode("utf-8"))


class IntegrateCache:
    """In-memory view of the cache file. ``get`` / ``put`` mark fingerprints as live; ``save``
    writes only the live ones, pruning entries for intervals that no longer exist."""

    def __init__(self, entries: dict[str, list[dict[str, Any]]] | None = None) -> None:
        self._entries: dict[str, list[dict[str, Any]]] = dict(entries or {})
        self._live: dict[str, list[dict[str, Any]]] = {}

    @classmethod
    def load(cls, path: Path) -> IntegrateCache:
        """Read the cache file; a missing, corrupt or foreign-version file is an empty cache."""
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        if not isinstance(data, dict) or data.get("version") != CACHE_VERSION:
            return cls()
        entries = data.get("entries")
        if not isinstance(entries, dict):
            return cls()
        return cls({fp: rows for fp, rows in entries.items() if _valid_rows(rows)})

    def __len__(self) -> int:
        return len(self._entries)

    def get(self, fingerprint: str) -> list[dict[str, Any]] | None:
        """Cached rows for ``fingerprint`` (copies), or ``None``; a hit marks the entry live."""
        rows = self._entries.get(fingerprint)
        if rows is None:
            return None
        self._live[fingerprint] = rows
        return [dict(row) for row in rows]

    def put(self, fingerprint: str, rows: list[dict[str, Any]]) -> None:
        snapshot = [dict(row) for row in rows]
        self._entries[fingerprint] = snapshot
        self._live[fingerprint] = snapshot

    def save(self, path: Path, *, provider: str, prompt_version: str) -> None:
        payload = {
            "version": CACHE_VERSION,
            "provider": provider,
            "prompt_version": prompt_version,
            "entries": self._live,
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(path)


def _valid_rows(rows: Any) -> bool:
    return isinstance(rows, list) and all(
        isinstance(row, dict) and set(ROW_KEYS) <= set(row) for row in rows
    )
