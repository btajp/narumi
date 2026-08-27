"""Strict response adapters for gaia-library contract major 1.

The peer's contracts remain authoritative. These models validate the structures consumed
by narumi without stripping compatible, additive fields from the returned dictionaries.
Missing arrays are errors, not empty search results.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

from narumi.errors import ContractMismatchError

Kind = Literal["fact", "inference"]
EntityType = Literal["person", "organization", "engagement", "interaction", "entity"]
RefTargetType = Literal["person", "organization", "engagement", "interaction", "entity", "fact"]


class _Response(BaseModel):
    model_config = ConfigDict(strict=True, extra="allow", allow_inf_nan=False)

    @model_validator(mode="before")
    @classmethod
    def absent_fields_are_not_null(cls, value: Any) -> Any:
        # Contract-v1 optional properties may be omitted, but are not nullable.
        if isinstance(value, dict) and any(
            name in value and value[name] is None for name in cls.model_fields
        ):
            raise ValueError("known Gaia response fields cannot be null")
        return value


class Alias(_Response):
    model_config = ConfigDict(extra="forbid")

    alias: str
    kind: str | None = None


class Person(_Response):
    id: int
    name: str
    aliases: list[Alias]
    org_id: int | None = None
    org_name: str | None = None
    role: str | None = None
    first_met: str | None = None
    last_seen: str | None = None


class Organization(_Response):
    id: int
    name: str
    kind: str | None = None


class Engagement(_Response):
    id: int
    name: str
    scope: str
    org_id: int | None = None
    org_name: str | None = None
    status: str | None = None
    started_at: str | None = None
    ended_at: str | None = None


class EngagementPerson(_Response):
    person: Person
    role: str | None = None


class Fact(_Response):
    id: int
    entity_type: EntityType
    entity_id: int
    statement: str
    kind: Kind
    scope: str
    created_at: str
    predicate: str | None = None
    value: str | None = None
    valid_from: str | None = None
    superseded_by: int | None = None


class Reference(_Response):
    id: int
    target_type: RefTargetType
    target_id: int
    system: str
    uri: str
    note: str
    scope: str
    created_at: str
    title: str | None = None
    snapshot: str | None = None
    last_verified: str | None = None


class GlossaryTerm(_Response):
    id: int
    term: str
    scope: str
    reading: str | None = None
    definition: str | None = None
    engagement_id: int | None = None


class Interaction(_Response):
    id: int
    kind: str
    occurred_at: str
    summary: str
    scope: str
    person_ids: list[int]
    engagement_id: int | None = None


class SearchEntity(_Response):
    type: EntityType
    id: int
    name: str
    summary: str
    score: float
    matched_on: list[str]
    facts: list[Fact]
    refs: list[Reference]


class SearchContext(_Response):
    query: str
    scopes: list[str]
    cross_scope: bool
    entities: list[SearchEntity]
    glossary: list[GlossaryTerm]
    interactions: list[Interaction]
    hints: list[str]


class EngagementDetails(_Response):
    engagement: Engagement
    people: list[EngagementPerson]
    facts: list[Fact]
    refs: list[Reference]
    glossary: list[GlossaryTerm]
    interactions: list[Interaction]
    organization: Organization | None = None


class Glossary(_Response):
    terms: list[GlossaryTerm]
    vocabulary_hints: list[str]


class SpeakerCandidate(_Response):
    person_id: int
    name: str
    confidence: float
    reason: str


class Speaker(_Response):
    input: str
    normalized: str
    status: Literal["matched", "ambiguous", "unmatched"]
    confidence: float
    candidates: list[SpeakerCandidate]
    person: Person | None = None

    @model_validator(mode="after")
    def matched_person_is_present(self) -> Speaker:
        if self.status == "matched" and self.person is None:
            raise ValueError("a matched speaker must include person")
        return self


class Speakers(_Response):
    results: list[Speaker]


class ProposalResult(_Response):
    proposal_id: int
    status: Literal["pending", "approved", "rejected"]
    duplicate: bool


class Protocol(_Response):
    transports: list[str]


class SearchCapabilities(_Response):
    fts: str


class Capabilities(_Response):
    tools: list[str]
    resolvers: list[str]
    search: SearchCapabilities


class ClientIdentity(_Response):
    name: str
    role: str
    default_scope: str | None = None


class ServerInfo(_Response):
    name: str
    version: str
    contract_version: str
    protocol: Protocol
    capabilities: Capabilities
    client: ClientIdentity


RESPONSE_MODELS: dict[str, type[_Response]] = {
    "get_server_info": ServerInfo,
    "search_context": SearchContext,
    "get_engagement": EngagementDetails,
    "get_glossary": Glossary,
    "resolve_speakers": Speakers,
    "propose_update": ProposalResult,
}


def validate_response(tool: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Validate without normalizing or discarding any of the peer's output fields."""
    try:
        RESPONSE_MODELS[tool].model_validate(payload)
    except ValidationError as err:
        # Never include the peer's raw values (which may contain credentials or meeting data).
        issues = [
            {"path": list(issue["loc"]), "type": issue["type"]}
            for issue in err.errors(include_input=False, include_context=False, include_url=False)
        ]
        raise ContractMismatchError(
            f"gaia-library returned an invalid {tool} response",
            details={"tool": tool, "validation": issues},
        ) from None
    return payload
