"""Closed, versioned transcription ledger validation without provider diagnostics."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from narumi.transcribe._storage import storage_error

VERSION = 1
_HASH = re.compile(r"^[0-9a-f]{64}$")
_ATTEMPT = re.compile(r"^[0-9a-f]{32}$")
_ATTEMPT_FIELDS = {"state", "attempt_id", "epoch", "was_unknown"}
UNKNOWN_STATES = frozenset({"pending", "unknown"})


def is_hash(value: Any) -> bool:
    return isinstance(value, str) and _HASH.fullmatch(value) is not None


def validate_entry(entry: Any) -> None:
    if not isinstance(entry, dict):
        raise storage_error()
    state = entry.get("state")
    if not isinstance(state, str):
        raise storage_error()
    if state == "unattempted" and set(entry) == {"state"}:
        return
    fields = _ATTEMPT_FIELDS | ({"result_sha256"} if state == "succeeded" else set())
    if (
        state not in {"pending", "unknown", "known_failed", "succeeded"}
        or set(entry) != fields
        or not isinstance(entry["attempt_id"], str)
        or _ATTEMPT.fullmatch(entry["attempt_id"]) is None
        or type(entry["epoch"]) is not int
        or entry["epoch"] < 0
        or type(entry["was_unknown"]) is not bool
        or (state == "known_failed" and entry["was_unknown"])
        or (state == "succeeded" and not is_hash(entry["result_sha256"]))
    ):
        raise storage_error()


def validate_document(document: Any) -> dict[str, Any]:
    if (
        not isinstance(document, dict)
        or set(document) != {"version", "plans", "entries"}
        or type(document["version"]) is not int
        or document["version"] != VERSION
        or not isinstance(document["plans"], dict)
        or not isinstance(document["entries"], dict)
    ):
        raise storage_error()
    for fingerprint, entry in document["entries"].items():
        if not is_hash(fingerprint):
            raise storage_error()
        validate_entry(entry)
    referenced: set[str] = set()
    for fingerprint, chunks in document["plans"].items():
        if (
            not is_hash(fingerprint)
            or not isinstance(chunks, list)
            or not 1 <= len(chunks) <= 144
            or any(not is_hash(chunk) for chunk in chunks)
            or len(set(chunks)) != len(chunks)
        ):
            raise storage_error()
        referenced.update(chunks)
    if referenced != set(document["entries"]):
        raise storage_error()
    return document


def result_name(fingerprint: str, entry: dict[str, Any]) -> str:
    """Derive paths only from validated local identifiers, never stored paths."""
    if not is_hash(fingerprint):
        raise storage_error()
    validate_entry(entry)
    if entry["state"] == "unattempted":
        raise storage_error()
    return f"{fingerprint}-{entry['attempt_id']}.json"


def validate_stored_plan(payload: Any, root: Path) -> list[str]:
    """Reconstruct only paths derived from hashes and recheck all plan invariants."""
    from narumi.transcribe.chunks import (
        CHUNKER_VERSION,
        TranscriptionChunk,
        TranscriptionPlan,
    )

    plan_fields = {
        "version",
        "chunker_version",
        "input_fingerprint",
        "params",
        "total_samples",
        "chunks",
    }
    chunk_fields = {
        "track",
        "index",
        "start_sample",
        "end_sample",
        "sample_rate",
        "source_sha256",
        "audio_sha256",
        "fingerprint",
    }
    if (
        not isinstance(payload, dict)
        or set(payload) != plan_fields
        or type(payload["version"]) is not int
        or payload["version"] != VERSION
        or payload["chunker_version"] != CHUNKER_VERSION
        or not isinstance(payload["chunks"], list)
    ):
        raise storage_error()
    chunks = []
    for item in payload["chunks"]:
        if (
            not isinstance(item, dict)
            or set(item) != chunk_fields
            or not is_hash(item["fingerprint"])
        ):
            raise storage_error()
        chunks.append(
            TranscriptionChunk(
                **item,
                path=root
                / "preprocess"
                / "transcription"
                / "chunks"
                / f"{item['fingerprint']}.wav",
                _bundle_root=root,
            )
        )
    try:
        TranscriptionPlan(
            payload["input_fingerprint"], tuple(chunks), payload["params"], payload["total_samples"]
        ).validate()
    except Exception:
        raise storage_error() from None
    return [chunk.fingerprint for chunk in chunks]
