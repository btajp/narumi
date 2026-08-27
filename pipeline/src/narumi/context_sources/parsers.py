"""Deterministic parsers for external transcript sources (話者分離 第 4 層の入力).

:func:`parse_context` turns the raw text of a registered context source into a
:class:`~narumi.models.Transcript` (``kind="external"``, ``source_id="ext-<context_id>"``), or
``None`` when the payload is not a transcript. Everything here is a fixed procedure — no LLM,
no network — so the same stored source always yields the same transcript (絶対原則 2).

Supported formats (see :func:`detect_format`):

- ``vtt``: WebVTT as exported by Zoom / Teams / Meet — cue timestamps, optional
  ``Speaker Name: text`` payload prefixes and ``<v Speaker Name>`` voice tags
- ``srt``: SubRip cues (comma decimals, optional ``Speaker Name: text`` prefixes)
- ``zoom_txt``: Zoom's plain transcript (``HH:MM:SS speaker: text`` lines)
- ``plain``: free text with optional ``[hh:mm:ss]`` line prefixes; without any timestamp the
  segments get index-spaced times (``time_offset`` stays 0) and the engine params note the low
  confidence. ``notion_ai_minutes`` always gets this plain treatment (no speaker extraction —
  the text may be summary-level prose, not verbatim speech).

Only transcript source types (:data:`TRANSCRIPT_SOURCE_TYPES`) are parsed; a ``document`` /
``chat_log`` / … source returns ``None`` so prose never pollutes alignment as a fake transcript.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from narumi.errors import InvalidArgumentError
from narumi.models import EngineInfo, Segment, Transcript

PARSER_VERSION = "1"

FORMAT_VTT = "vtt"
FORMAT_SRT = "srt"
FORMAT_ZOOM_TXT = "zoom_txt"
FORMAT_PLAIN = "plain"

TRANSCRIPT_SOURCE_TYPES = frozenset(
    {"notion_ai_minutes", "zoom_transcript", "meet_transcript", "teams_transcript"}
)
"""``source_type`` values that may carry a transcript (mirrors the register_context contract)."""

INDEX_SPACING_SEC = 5.0
"""Nominal segment length when a source has no usable timestamps (index-spaced times)."""

DEFAULT_TAIL_SEC = 5.0
"""End time of the last Zoom-txt segment: its start plus this (the format has no end times)."""

_TIMESTAMP = r"(?:(\d{1,3}):)?([0-5]?\d):([0-5]\d)[.,](\d{1,3})"
_CUE_TIMING_RE = re.compile(rf"^\s*{_TIMESTAMP}\s*-->\s*{_TIMESTAMP}")
_ZOOM_LINE_RE = re.compile(r"^\s*(\d{1,3}):([0-5]\d):([0-5]\d)\s+(\S[^:：]{0,79}?)\s*[:：]\s?(.*)$")
_PLAIN_TS_RE = re.compile(r"^\s*\[(?:(\d{1,3}):)?([0-5]?\d):([0-5]\d)\]\s*(.*)$")
_VOICE_TAG_RE = re.compile(r"<v(?:\.[^\s>]*)?\s+([^>]+)>")
_TAG_RE = re.compile(r"<[^>]*>")
_SPEAKER_PREFIX_RE = re.compile(r"^\s*([^:：\s\d][^:：]{0,59}?)\s*[:：]\s*(.*)$")
_BLOCK_SPLIT_RE = re.compile(r"\n\s*\n")
_SKIP_BLOCKS = ("NOTE", "STYLE", "REGION")


@dataclass(frozen=True)
class ParsedSegment:
    """One parsed utterance before it becomes a :class:`~narumi.models.Segment`."""

    start: float
    end: float
    text: str
    speaker: str | None = None


def detect_format(text: str) -> str | None:
    """Best-effort format of ``text``: vtt / srt / zoom_txt / plain, ``None`` when empty.

    A ``WEBVTT`` header wins; otherwise the first cue timing line decides between srt (comma
    decimals) and headerless vtt (dot decimals); otherwise a majority of ``HH:MM:SS speaker:``
    lines means zoom_txt; any other non-empty text is plain.
    """
    if not isinstance(text, str):
        return None
    stripped = text.lstrip("\ufeff").strip()
    if not stripped:
        return None
    if stripped.startswith("WEBVTT"):
        return FORMAT_VTT
    lines = [line.strip() for line in stripped.splitlines() if line.strip()]
    for line in lines[:20]:
        if "-->" in line and _CUE_TIMING_RE.match(line):
            return FORMAT_SRT if "," in line.split("-->", 1)[0] else FORMAT_VTT
    zoom_lines = sum(1 for line in lines if _ZOOM_LINE_RE.match(line))
    if zoom_lines and zoom_lines * 2 >= len(lines):
        return FORMAT_ZOOM_TXT
    return FORMAT_PLAIN


def parse_context(source_type: str, text: str, *, context_id: str) -> Transcript | None:
    """Parse a stored context source into an external transcript, or ``None``.

    ``None`` — not an error — when ``source_type`` is not a transcript kind, the text is empty,
    or nothing parseable remains. The result has ``source_id = "ext-<context_id>"``, segment ids
    ``ext-<context_id>:<index>``, ``engine.name = "parser-<format>"`` and speaker names on the
    segments whenever the source carries them.
    """
    if not context_id:
        raise InvalidArgumentError("context_id is required to name the transcript source")
    if source_type not in TRANSCRIPT_SOURCE_TYPES:
        return None
    fmt = detect_format(text)
    if fmt is None:
        return None
    if source_type == "notion_ai_minutes" and fmt == FORMAT_ZOOM_TXT:
        # Notion AI minutes are prose; a clock-looking prefix is not a Zoom transcript line.
        fmt = FORMAT_PLAIN
    parsed, params = _PARSERS[fmt](text)
    if not parsed:
        return None
    source_id = f"ext-{context_id}"
    return Transcript(
        source_id=source_id,
        kind="external",
        engine=EngineInfo(
            name=f"parser-{fmt}", version=PARSER_VERSION, params={"format": fmt, **params}
        ),
        segments=[
            Segment(
                id=f"{source_id}:{i}",
                start=item.start,
                end=max(item.start, item.end),
                text=item.text,
                speaker=item.speaker,
            )
            for i, item in enumerate(parsed)
        ],
    )


# ---------------------------------------------------------------------------- cue formats
def _parse_vtt(text: str) -> tuple[list[ParsedSegment], dict[str, Any]]:
    return _parse_cues(text), {"timestamps": "explicit"}


def _parse_srt(text: str) -> tuple[list[ParsedSegment], dict[str, Any]]:
    return _parse_cues(text), {"timestamps": "explicit"}


def _parse_cues(text: str) -> list[ParsedSegment]:
    """Shared WebVTT / SRT cue-block parser (blank-line separated; malformed blocks skipped)."""
    normalized = text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    segments: list[ParsedSegment] = []
    for block in _BLOCK_SPLIT_RE.split(normalized):
        lines = [line for line in block.split("\n") if line.strip()]
        if lines and lines[0].strip().startswith("WEBVTT"):
            lines = lines[1:]  # tolerate a header glued to the first cue
        if not lines or lines[0].strip().startswith(_SKIP_BLOCKS):
            continue
        timing_at = next((i for i, line in enumerate(lines) if "-->" in line), None)
        if timing_at is None:
            continue
        match = _CUE_TIMING_RE.match(lines[timing_at])
        if match is None:
            continue
        start = _cue_seconds(match, 0)
        end = max(start, _cue_seconds(match, 4))
        payload = " ".join(line.strip() for line in lines[timing_at + 1 :]).strip()
        if payload:
            segments.extend(_cue_payload_segments(start, end, payload))
    return segments


def _cue_seconds(match: re.Match[str], offset: int) -> float:
    hours, minutes, seconds, decimals = match.groups()[offset : offset + 4]
    millis = int(decimals.ljust(3, "0"))
    return round(int(hours or 0) * 3600 + int(minutes) * 60 + int(seconds) + millis / 1000, 3)


def _cue_payload_segments(start: float, end: float, payload: str) -> list[ParsedSegment]:
    """Split one cue payload into segments: one per ``<v Name>`` voice, else a single segment."""
    voices = list(_VOICE_TAG_RE.finditer(payload))
    if voices:
        segments: list[ParsedSegment] = []
        for i, voice in enumerate(voices):
            until = voices[i + 1].start() if i + 1 < len(voices) else len(payload)
            body = _TAG_RE.sub("", payload[voice.end() : until]).strip()
            name = voice.group(1).strip()
            if body:
                segments.append(ParsedSegment(start, end, body, name or None))
        return segments
    body = _TAG_RE.sub("", payload).strip()
    if not body:
        return []
    speaker, body = _split_speaker_prefix(body)
    return [ParsedSegment(start, end, body, speaker)]


def _split_speaker_prefix(text: str) -> tuple[str | None, str]:
    """``"Speaker Name: text"`` → ``("Speaker Name", "text")``; unchanged when no prefix.

    The name must not start with a digit (so a clock like ``13:00`` is never a speaker) and both
    halves must be non-empty.
    """
    match = _SPEAKER_PREFIX_RE.match(text)
    if match is None:
        return None, text
    name, rest = match.group(1).strip(), match.group(2).strip()
    if not name or not rest:
        return None, text
    return name, rest


# ---------------------------------------------------------------------------- zoom txt
def _parse_zoom_txt(text: str) -> tuple[list[ParsedSegment], dict[str, Any]]:
    """``HH:MM:SS speaker: text`` lines; a segment ends where the next one starts."""
    rows: list[tuple[float, str | None, str]] = []
    for line in text.splitlines():
        match = _ZOOM_LINE_RE.match(line)
        if match is None:
            continue
        start = float(int(match.group(1)) * 3600 + int(match.group(2)) * 60 + int(match.group(3)))
        speaker = match.group(4).strip() or None
        body = match.group(5).strip()
        if body:
            rows.append((start, speaker, body))
    segments: list[ParsedSegment] = []
    for i, (start, speaker, body) in enumerate(rows):
        if i + 1 < len(rows) and rows[i + 1][0] > start:
            end = rows[i + 1][0]
        else:
            end = start + DEFAULT_TAIL_SEC
        segments.append(ParsedSegment(start, end, body, speaker))
    return segments, {"timestamps": "explicit"}


# ---------------------------------------------------------------------------- plain text
def _parse_plain(text: str) -> tuple[list[ParsedSegment], dict[str, Any]]:
    """One segment per non-empty line; ``[hh:mm:ss]`` / ``[mm:ss]`` prefixes set start times.

    Without any timestamp the lines get index-spaced times and the params say so (low
    confidence): alignment may then anchor the source on text alone, and ``time_offset`` stays 0.
    Lines between timestamps inherit the span of the previous timestamped segment. No speaker
    extraction — plain sources (Notion AI minutes included) may be summary prose.
    """
    entries: list[tuple[float | None, str]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        match = _PLAIN_TS_RE.match(stripped)
        if match is None:
            entries.append((None, stripped))
            continue
        start = float(
            int(match.group(1) or 0) * 3600 + int(match.group(2)) * 60 + int(match.group(3))
        )
        body = match.group(4).strip()
        if body:
            entries.append((start, body))
    if not entries:
        return [], {}
    timed = sum(1 for start, _ in entries if start is not None)
    if timed == 0:
        segments = [
            ParsedSegment(round(i * INDEX_SPACING_SEC, 3), round((i + 1) * INDEX_SPACING_SEC, 3), b)
            for i, (_, b) in enumerate(entries)
        ]
        return segments, {"timestamps": "none", "confidence": "low"}
    segments = []
    for i, (start, body) in enumerate(entries):
        if start is None:
            previous = segments[-1] if segments else ParsedSegment(0.0, INDEX_SPACING_SEC, "")
            segments.append(ParsedSegment(previous.start, previous.end, body))
            continue
        next_start = next((s for s, _ in entries[i + 1 :] if s is not None), None)
        if next_start is None or next_start <= start:
            next_start = start + INDEX_SPACING_SEC
        segments.append(ParsedSegment(start, next_start, body))
    params = {"timestamps": "explicit" if timed == len(entries) else "partial"}
    return segments, params


_PARSERS: dict[str, Callable[[str], tuple[list[ParsedSegment], dict[str, Any]]]] = {
    FORMAT_VTT: _parse_vtt,
    FORMAT_SRT: _parse_srt,
    FORMAT_ZOOM_TXT: _parse_zoom_txt,
    FORMAT_PLAIN: _parse_plain,
}
