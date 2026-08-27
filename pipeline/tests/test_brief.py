"""Meeting brief: real Gaia read shapes, scoped cache identity, and prioritized injection."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest
from narumi.brief import (
    BRIEF_ARTIFACT_KEY,
    BRIEF_PATH,
    Brief,
    Participant,
    build_brief,
    inject_brief,
)
from narumi.bundle import Bundle, ContextRecord
from narumi.errors import ContractMismatchError, EngineUnavailableError, NotFoundError
from narumi.models import MeetingConfig


def make_bundle(tmp_path: Path) -> Bundle:
    bundle = Bundle.create(
        tmp_path / "meetings",
        meeting_name="定例ミーティング",
        engagement="acme",
        scope="client-a",
        config=MeetingConfig(vocab_hints=["Kubernetes", "SSO"], self_name="岡村"),
    )
    return bundle


class StubGaia:
    """Actual public response shapes; transport/schema validation lives in test_gaia_client."""

    def __init__(self) -> None:
        self.url = "http://127.0.0.1:8877/mcp"
        self.calls: list[tuple[str, dict]] = []
        self.client = {"name": "narumi", "role": "agent", "default_scope": "client-a"}
        self.failure: Exception | None = None
        self.glossary = {
            "terms": [
                {
                    "id": 1,
                    "term": "SCIM",
                    "reading": "スキム",
                    "definition": "規格",
                    "scope": "client-a",
                },
                {"id": 2, "term": "Kubernetes", "scope": "client-a"},
            ],
            "vocabulary_hints": ["SCIM", "スキム", "Kubernetes", "田中太郎", "Tanaka"],
        }
        fact = {
            "id": 3,
            "entity_type": "engagement",
            "entity_id": 42,
            "statement": "acme は SSO 移行の途中",
            "kind": "fact",
            "scope": "client-a",
            "created_at": "2026-08-20T01:00:00Z",
        }
        ref = {
            "id": 4,
            "target_type": "engagement",
            "target_id": 42,
            "system": "notion",
            "uri": "notion://page/prev",
            "title": "前回議事録",
            "note": "8/20 会議の決定事項",
            "snapshot": "移行対象は三部署",
            "scope": "client-a",
            "created_at": "2026-08-20T01:00:00Z",
        }
        interaction = {
            "id": 5,
            "kind": "meeting",
            "occurred_at": "2026-08-20T01:00:00Z",
            "summary": "前回はリリース日を 9/10 に決めた",
            "engagement_id": 42,
            "scope": "client-a",
            "person_ids": [9],
        }
        self.engagement = {
            "engagement": {"id": 42, "name": "acme", "scope": "client-a", "status": "active"},
            "people": [
                {
                    "person": {
                        "id": 9,
                        "name": "田中太郎",
                        "aliases": [{"alias": "Tanaka", "kind": "romaji"}],
                        "org_name": "acme",
                        "role": "情シス",
                    },
                    "role": "contact",
                }
            ],
            "facts": [fact],
            "refs": [ref],
            "glossary": deepcopy(self.glossary["terms"]),
            "interactions": [interaction],
        }
        self.search = {
            "query": "定例ミーティング",
            "scopes": ["client-a"],
            "cross_scope": False,
            "entities": [
                {
                    "type": "engagement",
                    "id": 42,
                    "name": "acme",
                    "summary": "active",
                    "score": 3.0,
                    "matched_on": ["fact:3"],
                    "facts": [fact],
                    "refs": [ref],
                }
            ],
            "glossary": [
                {"id": 6, "term": "pHash", "definition": "画像比較用", "scope": "client-a"}
            ],
            "interactions": [interaction],
            "hints": [],
        }

    def get_server_info(self, *, refresh=False):
        self.calls.append(("get_server_info", {"refresh": refresh}))
        if self.failure is not None:
            raise self.failure
        return {
            "name": "gaia_library",
            "version": "0.1.0",
            "contract_version": "1.0.0",
            "protocol": {"transports": ["streamable_http"]},
            "capabilities": {
                "tools": ["search_context", "get_engagement", "get_glossary"],
                "resolvers": ["file"],
                "search": {"fts": "trigram"},
            },
            "client": deepcopy(self.client),
        }

    def require_capabilities(self, *tools):
        self.calls.append(("require_capabilities", {"tools": tools}))

    def get_engagement(self, name, *, scope=None):
        self.calls.append(("get_engagement", {"name": name, "scope": scope}))
        return deepcopy(self.engagement)

    def get_glossary(self, engagement=None, *, scope=None, engagement_id=None):
        assert engagement is None  # labels must be resolved by the scoped get_engagement read
        self.calls.append(("get_glossary", {"engagement_id": engagement_id, "scope": scope}))
        return deepcopy(self.glossary)

    def search_context(self, query, *, scope=None):
        self.calls.append(("search_context", {"query": query, "scope": scope}))
        return deepcopy(self.search)


# ---------------------------------------------------------------------------- build_brief
def test_local_only_brief_is_never_an_error(tmp_path: Path):
    bundle = make_bundle(tmp_path)
    brief = build_brief(bundle, None)
    assert brief.vocab_hints == ["Kubernetes", "SSO"]
    assert brief.participants == [Participant(name="岡村", note="記録者（本人）")]
    assert brief.previous_points == [] and brief.background == [] and brief.sources == []
    # brief.json is always written and recorded as a run_stage artifact
    assert bundle.abspath(BRIEF_PATH).exists()
    record = bundle.manifest.artifacts[BRIEF_ARTIFACT_KEY]
    assert record.params == {"gaia": False, "version": 3}
    assert record.producer.name == "brief"
    assert record.producer.version == "3"
    assert "config" in record.inputs


def test_gaia_results_merge_into_brief(tmp_path: Path):
    bundle = make_bundle(tmp_path)
    gaia = StubGaia()
    brief = build_brief(bundle, gaia)  # type: ignore[arg-type]
    # Local hints first, then server vocabulary hints, readings and search glossary.
    assert brief.vocab_hints == [
        "Kubernetes",
        "SSO",
        "SCIM",
        "スキム",
        "田中太郎",
        "Tanaka",
        "pHash",
    ]
    names = [p.name for p in brief.participants]
    assert names == ["岡村", "田中太郎"]
    tanaka = brief.participants[1]
    assert tanaka.aliases == ["Tanaka"] and tanaka.person_id == 9
    assert tanaka.note == "acme / 情シス / 案件での役割: contact"
    assert brief.previous_points == ["前回はリリース日を 9/10 に決めた"]
    assert brief.background == [
        "SCIM: 規格",
        "acme / active",
        "acme は SSO 移行の途中",
        "参照の登録時要約（前回議事録）: 移行対象は三部署",
        "pHash: 画像比較用",
    ]
    assert len(brief.sources) == 1
    source = brief.sources[0]
    assert (source.system, source.uri, source.note) == (
        "notion",
        "notion://page/prev",
        "8/20 会議の決定事項",
    )
    assert source.title == "前回議事録" and source.snapshot == "移行対象は三部署"
    assert source.ref_id == 4 and source.scope == "client-a"
    assert ("get_engagement", {"name": "acme", "scope": "client-a"}) in gaia.calls
    assert ("get_glossary", {"engagement_id": 42, "scope": "client-a"}) in gaia.calls
    assert ("search_context", {"query": "定例ミーティング", "scope": "client-a"}) in gaia.calls
    assert bundle.manifest.artifacts[BRIEF_ARTIFACT_KEY].params == {
        "gaia": True,
        "version": 3,
        "gaia_client": {"endpoint": gaia.url, "name": "narumi", "default_scope": "client-a"},
        "gaia_scope": "client-a",
    }
    assert brief.gaia_context == {
        "get_glossary": gaia.glossary,
        "get_engagement": gaia.engagement,
        "search_context": gaia.search,
    }


def test_build_brief_is_idempotent_until_inputs_change(tmp_path: Path):
    bundle = make_bundle(tmp_path)
    build_brief(bundle, None)
    first = bundle.manifest.artifacts[BRIEF_ARTIFACT_KEY]
    build_brief(bundle, None)
    assert bundle.manifest.artifacts[BRIEF_ARTIFACT_KEY] is first  # skipped, same record

    # config change → inputs change → rebuild picks up the new hint
    bundle.manifest.config.vocab_hints.append("pHash")
    bundle.save()
    brief = build_brief(bundle, None)
    assert "pHash" in brief.vocab_hints
    assert bundle.manifest.artifacts[BRIEF_ARTIFACT_KEY].inputs != first.inputs


def test_registered_context_sources_are_part_of_the_inputs(tmp_path: Path):
    bundle = make_bundle(tmp_path)
    build_brief(bundle, None)
    rel = "context/sources/ctx-abc.json"
    bundle.write_json(rel, {"context_id": "ctx-abc", "content": "外部トランスクリプト"})
    bundle.manifest.contexts.append(
        ContextRecord(
            context_id="ctx-abc",
            source_type="text",
            registered_at="2026-08-27T00:00:00Z",
            path=rel,
        )
    )
    bundle.save()
    build_brief(bundle, None)
    inputs = bundle.manifest.artifacts[BRIEF_ARTIFACT_KEY].inputs
    assert rel in inputs and len(inputs[rel]) == 64


def test_brief_json_round_trips(tmp_path: Path):
    bundle = make_bundle(tmp_path)
    built = build_brief(bundle, StubGaia())  # type: ignore[arg-type]
    assert Brief.model_validate(bundle.read_json(BRIEF_PATH)) == built


def test_gaia_cache_refreshes_identity_but_not_context_until_forced(tmp_path: Path):
    bundle = make_bundle(tmp_path)
    gaia = StubGaia()
    build_brief(bundle, gaia)  # type: ignore[arg-type]
    first = bundle.manifest.artifacts[BRIEF_ARTIFACT_KEY]
    gaia.calls.clear()
    build_brief(bundle, gaia)  # type: ignore[arg-type]
    assert bundle.manifest.artifacts[BRIEF_ARTIFACT_KEY] is first
    assert gaia.calls == [("get_server_info", {"refresh": True})]

    gaia.glossary["vocabulary_hints"].append("新しい用語")
    brief = build_brief(bundle, gaia, force=True)  # type: ignore[arg-type]
    assert "新しい用語" in brief.vocab_hints
    assert any(tool == "get_glossary" for tool, _ in gaia.calls)


@pytest.mark.parametrize("changed", ["scope", "endpoint", "client_name", "default_scope"])
def test_scope_and_nonsecret_identity_changes_invalidate_cache(tmp_path: Path, changed: str):
    bundle = make_bundle(tmp_path)
    if changed == "default_scope":
        bundle.manifest.scope = None
    gaia = StubGaia()
    build_brief(bundle, gaia)  # type: ignore[arg-type]
    first = bundle.manifest.artifacts[BRIEF_ARTIFACT_KEY]
    if changed == "scope":
        bundle.manifest.scope = "client-b"
    elif changed == "endpoint":
        gaia.url = "http://127.0.0.1:9988/mcp"
    elif changed == "client_name":
        gaia.client["name"] = "another-client"
    else:
        gaia.client["default_scope"] = "client-b"
    gaia.calls.clear()
    build_brief(bundle, gaia)  # type: ignore[arg-type]
    assert bundle.manifest.artifacts[BRIEF_ARTIFACT_KEY] is not first
    assert any(tool == "search_context" for tool, _ in gaia.calls)
    for tool, args in gaia.calls:
        if tool in {"get_engagement", "get_glossary", "search_context"}:
            assert args["scope"] == (bundle.manifest.scope or gaia.client["default_scope"])


def test_no_engagement_keeps_glossary_names_out_of_participants(tmp_path: Path):
    bundle = make_bundle(tmp_path)
    bundle.manifest.engagement = None
    bundle.manifest.scope = None
    gaia = StubGaia()
    gaia.search["entities"] = []
    brief = build_brief(bundle, gaia)  # type: ignore[arg-type]
    assert brief.participants == [Participant(name="岡村", note="記録者（本人）")]
    assert "田中太郎" in brief.vocab_hints
    assert not any(tool == "get_engagement" for tool, _ in gaia.calls)
    assert ("get_glossary", {"engagement_id": None, "scope": "client-a"}) in gaia.calls
    assert "get_engagement" not in brief.gaia_context


def test_entities_supply_people_and_preserve_fact_certainty(tmp_path: Path):
    bundle = make_bundle(tmp_path)
    gaia = StubGaia()
    person = {
        "type": "person",
        "id": 9,
        "name": "田中太郎",
        "summary": "情シス @ acme",
        "score": 2.0,
        "matched_on": ["alias"],
        "facts": [],
        "refs": [],
    }
    gaia.search["entities"].append(person)
    gaia.search["entities"].append({**person, "id": 10})  # Names alone are not identities.
    inferred = {
        **gaia.engagement["facts"][0],
        "id": 99,
        "kind": "inference",
        "statement": "移行予定",
    }
    gaia.engagement["facts"].append(inferred)
    brief = build_brief(bundle, gaia)  # type: ignore[arg-type]
    assert [p.person_id for p in brief.participants] == [None, 9, 10]
    assert "推測: 移行予定" in brief.background
    assert "移行予定" not in brief.background
    assert brief.gaia_context["get_engagement"]["facts"][-1]["kind"] == "inference"


@pytest.mark.parametrize("error", [EngineUnavailableError, ContractMismatchError, NotFoundError])
def test_configured_gaia_failure_is_not_hidden_by_cached_brief(tmp_path: Path, error):
    bundle = make_bundle(tmp_path)
    gaia = StubGaia()
    build_brief(bundle, gaia)  # type: ignore[arg-type]
    original = bundle.abspath(BRIEF_PATH).read_bytes()
    gaia.failure = error("configured Gaia failed")
    with pytest.raises(error):
        build_brief(bundle, gaia)  # type: ignore[arg-type]
    assert bundle.abspath(BRIEF_PATH).read_bytes() == original


def test_brief_never_persists_server_info_or_client_secret_material(tmp_path: Path):
    bundle = make_bundle(tmp_path)
    gaia = StubGaia()
    gaia.api_key = "test-private-key-never-persist"  # Not part of the cache identity.
    gaia.client["unexpected_secret"] = gaia.api_key
    build_brief(bundle, gaia)  # type: ignore[arg-type]
    for path in (bundle.abspath(BRIEF_PATH), bundle.abspath("manifest.json")):
        contents = path.read_text(encoding="utf-8")
        assert gaia.api_key not in contents and "unexpected_secret" not in contents
        assert "get_server_info" not in contents


# ---------------------------------------------------------------------------- inject_brief
def full_brief() -> Brief:
    return Brief(
        vocab_hints=["SCIM", "pHash"],
        participants=[Participant(name="田中太郎", aliases=["Tanaka"], note="acme 情シス")],
        previous_points=["リリース日は 9/10"],
        background=["SSO 移行中"],
    )


def test_inject_renders_sections_in_priority_order():
    text = inject_brief(full_brief(), budget_chars=10_000)
    assert text.index("## 語彙") < text.index("## 参加者") < text.index("## 前回要点")
    assert text.index("## 前回要点") < text.index("## 背景")
    assert "- SCIM" in text and "- pHash" in text
    assert "- 田中太郎（別名: Tanaka） — acme 情シス" in text
    assert "- リリース日は 9/10" in text and "- SSO 移行中" in text
    assert len(text) <= 10_000


def test_inject_truncates_tail_first():
    brief = full_brief()
    # budget for the vocab header + first item only
    budget = len("## 語彙") + 1 + len("- SCIM")
    text = inject_brief(brief, budget_chars=budget)
    assert text == "## 語彙\n- SCIM"
    # once a line is cut, lower-priority sections never jump the queue,
    # even if a later line would have fit into the remaining budget
    assert "参加者" not in inject_brief(brief, budget_chars=budget + 3)


def test_inject_skips_empty_sections_and_respects_budget_exactly():
    brief = Brief(vocab_hints=[], participants=[], previous_points=["要点"], background=[])
    text = inject_brief(brief, budget_chars=10_000)
    assert text == "## 前回要点\n- 要点"
    for budget in range(0, len(text) + 1):
        assert len(inject_brief(brief, budget_chars=budget)) <= budget
    assert inject_brief(brief, budget_chars=0) == ""
    assert inject_brief(Brief(), budget_chars=100) == ""
