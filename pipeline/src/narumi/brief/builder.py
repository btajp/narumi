"""Meeting brief v1: build ``context/brief.json`` and render it for prompt injection.

コンテキスト注入 v1 (AGENTS.md): query gaia-library when available, merge with local bundle data
(``manifest.config.vocab_hints`` / ``self_name``), persist the brief to ``context/brief.json``
and inject it into prompts in priority order — 語彙 > 参加者 > 前回要点 > 背景 — truncated to a
character budget the caller scales from the provider's capability profile.

gaia-library is optional: ``build_brief(bundle, None)`` builds the brief from local data only and
is never an error. When a client *is* passed but the server is unreachable, the error propagates
(no silent fallback): the caller chose gaia explicitly and a silently thinner brief would break
the "same inputs → same version" promise.

The brief is a ``run_stage`` artifact (key ``context/brief``), so it is idempotent over the
manifest config subset and the registered context sources. gaia responses are *not* part of the
inputs — re-querying gaia for fresh context is an explicit ``force=True`` regeneration.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from narumi.bundle import Bundle, StageResult, sha256_file, sha256_params
from narumi.gaia import GaiaClient

BRIEF_ARTIFACT_KEY = "context/brief"
BRIEF_PATH = "context/brief.json"
BRIEF_VERSION = 1
PRODUCER = ("brief", "1")

_PREVIOUS_KINDS = frozenset({"minutes", "previous_minutes", "meeting"})
_PERSON_KIND = "person"


class Participant(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    aliases: list[str] = Field(default_factory=list)
    note: str | None = None


class BriefSource(BaseModel):
    """A reference the agent (or a human) can walk for more context. Not injected into prompts."""

    model_config = ConfigDict(extra="forbid")

    system: str
    uri: str
    note: str | None = None


class Brief(BaseModel):
    """会議ブリーフ: what the LLM stages get to know about the meeting before reading it."""

    model_config = ConfigDict(extra="forbid")

    vocab_hints: list[str] = Field(default_factory=list)
    """Config hints first, then gaia glossary terms; deduplicated, order-preserving."""
    participants: list[Participant] = Field(default_factory=list)
    previous_points: list[str] = Field(default_factory=list)
    background: list[str] = Field(default_factory=list)
    sources: list[BriefSource] = Field(default_factory=list)


def run_brief(
    bundle: Bundle, gaia: GaiaClient | None = None, *, force: bool = False
) -> StageResult:
    """Run the brief stage idempotently (see :func:`build_brief`) and return its result.

    This is the pipeline-facing entry point: ``narumi.pipeline`` wires it before transcription
    so the merged ``vocab_hints`` reach the transcription engine.
    """
    inputs = _brief_inputs(bundle)
    params = {"gaia": gaia is not None, "version": BRIEF_VERSION}

    def write(out_path: Path) -> None:
        bundle.write_json(BRIEF_PATH, _compose(bundle, gaia))

    return bundle.run_stage(
        BRIEF_ARTIFACT_KEY,
        inputs=inputs,
        params=params,
        producer=PRODUCER,
        output=BRIEF_PATH,
        fn=write,
        force=force,
    )


def build_brief(bundle: Bundle, gaia: GaiaClient | None = None, *, force: bool = False) -> Brief:
    """Build (or reuse) ``context/brief.json`` and return the :class:`Brief`.

    Runs as ``run_stage`` key ``context/brief``: inputs are the manifest config subset that feeds
    the brief plus the hash of every registered context source file; params record whether gaia
    was consulted. Without gaia the brief holds local data only (任意依存: never an error).
    """
    run_brief(bundle, gaia, force=force)
    return Brief.model_validate(bundle.read_json(BRIEF_PATH))


def load_brief(bundle: Bundle) -> Brief | None:
    """The recorded brief, or ``None`` when the brief stage never ran (or its file is gone)."""
    record = bundle.artifact(BRIEF_ARTIFACT_KEY)
    if record is None or not bundle.abspath(record.path).exists():
        return None
    return Brief.model_validate(bundle.read_json(record.path))


def inject_brief(brief: Brief, *, budget_chars: int) -> str:
    """Render the brief for prompt injection, truncated to ``budget_chars``.

    Sections come in priority order — 語彙 > 参加者 > 前回要点 > 背景 — one ``- `` line per item.
    Truncation is strictly tail-first: once a line does not fit, everything after it is dropped
    (lower-priority sections never jump the queue). ``sources`` are provenance, not prompt text.
    """
    if budget_chars <= 0:
        return ""
    sections: list[tuple[str, list[str]]] = [
        ("語彙", [f"- {hint}" for hint in brief.vocab_hints]),
        ("参加者", [f"- {_participant_line(p)}" for p in brief.participants]),
        ("前回要点", [f"- {point}" for point in brief.previous_points]),
        ("背景", [f"- {item}" for item in brief.background]),
    ]
    out: list[str] = []
    used = 0

    def fits(line: str) -> bool:
        return used + (1 if out else 0) + len(line) <= budget_chars

    def add(line: str) -> None:
        nonlocal used
        used += (1 if out else 0) + len(line)
        out.append(line)

    for title, items in sections:
        if not items:
            continue
        header = f"## {title}"
        # The header only earns its keep with at least one item after it.
        if used + (1 if out else 0) + len(header) + 1 + len(items[0]) > budget_chars:
            break
        add(header)
        for item in items:
            if not fits(item):
                return "\n".join(out)
            add(item)
    return "\n".join(out)


# ---------------------------------------------------------------------------- internals
def _brief_inputs(bundle: Bundle) -> dict[str, str]:
    config = bundle.manifest.config
    subset = {
        "meeting_name": bundle.manifest.meeting_name,
        "engagement": bundle.manifest.engagement,
        "language": config.language,
        "self_name": config.self_name,
        "vocab_hints": list(config.vocab_hints),
    }
    inputs = {"config": sha256_params(subset)}
    for record in bundle.manifest.contexts:
        path = bundle.abspath(record.path)
        if path.exists():
            inputs[record.path] = sha256_file(path)
    return inputs


def _compose(bundle: Bundle, gaia: GaiaClient | None) -> Brief:
    manifest = bundle.manifest
    config = manifest.config
    vocab: list[str] = list(dict.fromkeys(hint for hint in config.vocab_hints if hint.strip()))
    participants: list[Participant] = []
    previous: list[str] = []
    background: list[str] = []
    sources: list[BriefSource] = []

    if config.self_name:
        _add_participant(participants, config.self_name, [], "記録者（本人）")

    if gaia is not None:
        for term in gaia.get_glossary(manifest.engagement):
            name = str(term.get("term") or term.get("name") or "").strip()
            if not name:
                continue
            aliases = [str(a).strip() for a in term.get("aliases") or [] if str(a).strip()]
            note = str(term.get("note") or "").strip() or None
            if str(term.get("kind") or "").lower() == _PERSON_KIND:
                _add_participant(participants, name, aliases, note)
            else:
                for candidate in (name, *aliases):
                    if candidate not in vocab:
                        vocab.append(candidate)
        for ref in gaia.search_context(manifest.meeting_name, engagement=manifest.engagement):
            summary = str(ref.get("summary") or ref.get("note") or ref.get("title") or "").strip()
            if summary:
                kind = str(ref.get("kind") or "").lower()
                (previous if kind in _PREVIOUS_KINDS else background).append(summary)
            uri = str(ref.get("uri") or ref.get("url") or "").strip()
            if uri:
                sources.append(
                    BriefSource(
                        system=str(ref.get("system") or "gaia-library"),
                        uri=uri,
                        note=str(ref.get("title") or "").strip() or None,
                    )
                )

    return Brief(
        vocab_hints=vocab,
        participants=participants,
        previous_points=previous,
        background=background,
        sources=sources,
    )


def _add_participant(
    participants: list[Participant], name: str, aliases: list[str], note: str | None
) -> None:
    for existing in participants:
        if existing.name == name:
            existing.aliases.extend(a for a in aliases if a not in existing.aliases)
            if note and not existing.note:
                existing.note = note
            return
    participants.append(Participant(name=name, aliases=list(aliases), note=note))


def _participant_line(participant: Participant) -> str:
    line = participant.name
    if participant.aliases:
        line += f"（別名: {'、'.join(participant.aliases)}）"
    if participant.note:
        line += f" — {participant.note}"
    return line
