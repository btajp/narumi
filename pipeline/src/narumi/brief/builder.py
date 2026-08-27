"""Meeting brief: build ``context/brief.json`` and render it for prompt injection.

コンテキスト注入 v1 (AGENTS.md): query gaia-library when available, merge with local bundle data
(``manifest.config.vocab_hints`` / ``self_name``), persist the brief to ``context/brief.json``
and inject it into prompts in priority order — 語彙 > 参加者 > 前回要点 > 背景 — truncated to a
character budget the caller scales from the provider's capability profile.

gaia-library is optional: ``build_brief(bundle, None)`` builds the brief from local data only and
is never an error. When a client *is* passed but the server is unreachable, the error propagates
(no silent fallback): the caller chose gaia explicitly and a silently thinner brief would break
the "same inputs → same version" promise.

The brief is a ``run_stage`` artifact (key ``context/brief``), so it is idempotent over the
manifest config subset and the registered context sources. Gaia context responses are *not*
part of the inputs — fresh context is an explicit ``force=True`` regeneration.
Server identity is checked before cache reuse so a changed endpoint or default scope cannot
reuse another client's brief. This check also surfaces configured-but-unreachable servers.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from narumi.brief.gaia_context import enrich_brief
from narumi.brief.models import Brief, Participant
from narumi.brief.models import BriefSource as BriefSource
from narumi.bundle import Bundle, StageResult, sha256_file, sha256_params
from narumi.errors import ScopeDeniedError
from narumi.gaia import GaiaClient

BRIEF_ARTIFACT_KEY = "context/brief"
BRIEF_PATH = "context/brief.json"
BRIEF_VERSION = 3
PRODUCER = ("brief", "3")


def run_brief(
    bundle: Bundle, gaia: GaiaClient | None = None, *, force: bool = False
) -> StageResult:
    """Run the brief stage idempotently (see :func:`build_brief`) and return its result.

    This is the pipeline-facing entry point: ``narumi.pipeline`` wires it before transcription
    so the merged ``vocab_hints`` reach the transcription engine.
    """
    inputs = _brief_inputs(bundle)
    params: dict[str, Any] = {"gaia": gaia is not None, "version": BRIEF_VERSION}
    scope = None
    identity = None
    if gaia is not None:
        # Refresh even when this client has metadata cached: availability and its current
        # default scope must be checked before reusing previously persisted context.
        client_info = gaia.get_server_info(refresh=True)["client"]
        scope = _effective_scope(bundle, client_info)
        identity = {
            "endpoint": gaia.url,
            "name": client_info["name"],
            "default_scope": client_info.get("default_scope"),
        }
        params["gaia_client"] = identity
        params["gaia_scope"] = scope

    def write(out_path: Path) -> None:
        bundle.write_json(BRIEF_PATH, _compose(bundle, gaia, scope=scope, identity=identity))

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
        "scope": bundle.manifest.scope,
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


def _effective_scope(bundle: Bundle, client_info: dict[str, Any]) -> str:
    scope = bundle.manifest.scope
    if scope is None:
        scope = client_info.get("default_scope")
    if not isinstance(scope, str) or not scope.strip():
        raise ScopeDeniedError(
            "gaia-library brief requires a meeting scope or a client default scope"
        )
    return scope


def _compose(
    bundle: Bundle,
    gaia: GaiaClient | None,
    *,
    scope: str | None,
    identity: dict[str, Any] | None,
) -> Brief:
    manifest = bundle.manifest
    config = manifest.config
    brief = Brief(
        vocab_hints=list(dict.fromkeys(hint.strip() for hint in config.vocab_hints if hint.strip()))
    )
    if config.self_name:
        brief.participants.append(Participant(name=config.self_name, note="記録者（本人）"))
    if gaia is not None:
        if scope is None or identity is None:
            raise ScopeDeniedError("gaia-library brief requires a pinned connection and scope")
        enrich_brief(
            brief,
            gaia,
            meeting_name=manifest.meeting_name,
            engagement=manifest.engagement,
            scope=scope,
            identity=identity,
        )
    return brief


def _participant_line(participant: Participant) -> str:
    line = participant.name
    if participant.aliases:
        line += f"（別名: {'、'.join(participant.aliases)}）"
    if participant.note:
        line += f" — {participant.note}"
    return line
