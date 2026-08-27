"""Map Gaia's public read contracts to prioritized brief sections without losing evidence."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from narumi.brief.models import Brief, BriefSource, Participant
from narumi.errors import ContractMismatchError
from narumi.gaia import GaiaClient


def enrich_brief(
    brief: Brief,
    gaia: GaiaClient,
    *,
    meeting_name: str,
    engagement: str | None,
    scope: str,
    identity: dict[str, Any],
) -> None:
    """Keep exact tool responses as provenance and map their documented fields to the brief."""
    tools = ["get_glossary", "search_context"]
    if engagement is not None:
        tools.append("get_engagement")
    gaia.require_capabilities(*tools)
    _check_identity(gaia, identity)
    detail = gaia.get_engagement(engagement, scope=scope) if engagement is not None else None
    if detail is not None:
        _check_identity(gaia, identity)
    engagement_id = detail["engagement"]["id"] if detail is not None else None
    glossary = gaia.get_glossary(engagement_id=engagement_id, scope=scope)
    _check_identity(gaia, identity)
    search = gaia.search_context(meeting_name, scope=scope)
    _check_identity(gaia, identity)
    brief.gaia_context = deepcopy({"get_glossary": glossary, "search_context": search})
    if detail is not None:
        brief.gaia_context["get_engagement"] = deepcopy(detail)

    _extend(brief.vocab_hints, glossary["vocabulary_hints"])
    _terms(brief, glossary["terms"])
    if detail is not None:
        _engagement(brief, detail)
    for entity in search["entities"]:
        if entity["type"] == "person":
            _participant(
                brief,
                Participant(name=entity["name"], person_id=entity["id"], note=entity["summary"]),
            )
        elif entity["type"] == "interaction":
            _extend(brief.previous_points, [entity["summary"]])
        else:
            _extend(brief.background, [_headline(entity["name"], entity["summary"])])
        _facts(brief, entity["facts"])
        _refs(brief, entity["refs"])
    _terms(brief, search["glossary"])
    _interactions(brief, search["interactions"])


def _check_identity(gaia: GaiaClient, expected: dict[str, Any]) -> None:
    # This uses cached metadata, except after a session reset. Check every read so even
    # a transient identity change cannot be attributed to the initial cache identity.
    client = gaia.get_server_info()["client"]
    current = {
        "endpoint": gaia.url,
        "name": client["name"],
        "default_scope": client.get("default_scope"),
    }
    if current != expected:
        raise ContractMismatchError(
            "gaia-library connection identity changed while building the brief; retry"
        )


def _engagement(brief: Brief, detail: dict[str, Any]) -> None:
    engagement = detail["engagement"]
    organization = detail.get("organization")
    _extend(
        brief.background,
        [_headline(engagement["name"], engagement.get("org_name"), engagement.get("status"))],
    )
    if organization is not None:
        _extend(brief.background, [_headline(organization["name"], organization.get("kind"))])
    for member in detail["people"]:
        person = member["person"]
        notes = [person.get("org_name"), person.get("role")]
        if member.get("role"):
            notes.append(f"案件での役割: {member['role']}")
        _participant(
            brief,
            Participant(
                name=person["name"],
                person_id=person["id"],
                aliases=[alias["alias"] for alias in person["aliases"]],
                note=_headline(*notes) or None,
            ),
        )
    _facts(brief, detail["facts"])
    _refs(brief, detail["refs"])
    _terms(brief, detail["glossary"])
    _interactions(brief, detail["interactions"])


def _terms(brief: Brief, terms: list[dict[str, Any]]) -> None:
    for term in terms:
        _extend(brief.vocab_hints, [term["term"], term.get("reading")])
        if term.get("definition"):
            _extend(brief.background, [f"{term['term']}: {term['definition']}"])


def _facts(brief: Brief, facts: list[dict[str, Any]]) -> None:
    for fact in facts:
        # Inferences stay explicitly marked; a concise projection must not upgrade certainty.
        prefix = "推測: " if fact["kind"] == "inference" else ""
        if fact.get("superseded_by") is not None:
            prefix = "旧情報: " + prefix
        _extend(brief.background, [prefix + fact["statement"]])


def _refs(brief: Brief, refs: list[dict[str, Any]]) -> None:
    for ref in refs:
        source = BriefSource(
            system=ref["system"],
            uri=ref["uri"],
            note=ref["note"],
            title=ref.get("title"),
            snapshot=ref.get("snapshot"),
            ref_id=ref["id"],
            scope=ref["scope"],
        )
        if source not in brief.sources:
            brief.sources.append(source)
        # Snapshots are registered source summaries, not live connector reads.
        if source.snapshot:
            title = source.title or source.note or source.uri
            _extend(brief.background, [f"参照の登録時要約（{title}）: {source.snapshot}"])
        elif source.note:
            _extend(brief.background, [f"参照: {_headline(source.title, source.note)}"])


def _interactions(brief: Brief, interactions: list[dict[str, Any]]) -> None:
    for interaction in interactions:
        _extend(brief.previous_points, [interaction["summary"]])


def _participant(brief: Brief, participant: Participant) -> None:
    participant.aliases = list(dict.fromkeys(a for a in participant.aliases if a))
    for existing in brief.participants:
        has_id = existing.person_id is not None or participant.person_id is not None
        same_person = (
            existing.person_id == participant.person_id
            if has_id
            else existing.name == participant.name
        )
        if same_person:
            _extend(existing.aliases, participant.aliases)
            if participant.note and participant.note != existing.note:
                existing.note = _headline(existing.note, participant.note)
            return
    brief.participants.append(participant)


def _headline(*parts: str | None) -> str:
    return " / ".join(dict.fromkeys(part.strip() for part in parts if part and part.strip()))


def _extend(items: list[str], candidates: list[str | None]) -> None:
    for candidate in candidates:
        if candidate is not None and (value := candidate.strip()) and value not in items:
            items.append(value)
