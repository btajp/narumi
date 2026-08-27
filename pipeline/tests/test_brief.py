"""Meeting brief v1: build_brief (local-only and with gaia) and inject_brief (priority+budget)."""

from __future__ import annotations

from pathlib import Path

from narumi.brief import (
    BRIEF_ARTIFACT_KEY,
    BRIEF_PATH,
    Brief,
    Participant,
    build_brief,
    inject_brief,
)
from narumi.bundle import Bundle, ContextRecord
from narumi.models import MeetingConfig


def make_bundle(tmp_path: Path) -> Bundle:
    bundle = Bundle.create(
        tmp_path / "meetings",
        meeting_name="定例ミーティング",
        engagement="acme",
        config=MeetingConfig(vocab_hints=["Kubernetes", "SSO"], self_name="岡村"),
    )
    return bundle


class StubGaia:
    """Duck-typed stand-in for GaiaClient (the wire protocol is covered by test_gaia_client)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def get_glossary(self, engagement=None):
        self.calls.append(("get_glossary", engagement))
        return [
            {"term": "SCIM", "aliases": ["scim"], "note": "プロビジョニング規格"},
            {"term": "Kubernetes"},  # duplicate of a config hint
            {"term": "田中太郎", "kind": "person", "aliases": ["Tanaka"], "note": "acme 情シス"},
            {"term": ""},  # ignored
        ]

    def search_context(self, query, *, engagement=None, scope=None, limit=None):
        self.calls.append(("search_context", query, engagement))
        return [
            {
                "kind": "minutes",
                "summary": "前回はリリース日を 9/10 に決めた",
                "system": "notion",
                "uri": "notion://page/prev",
                "title": "前回議事録",
            },
            {
                "kind": "doc",
                "summary": "acme は SSO 移行の途中",
                "system": "box",
                "uri": "box://file/9",
            },
            {"kind": "doc"},  # no summary / uri → contributes nothing
        ]


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
    assert record.params == {"gaia": False, "version": 1}
    assert record.producer.name == "brief"
    assert "config" in record.inputs


def test_gaia_results_merge_into_brief(tmp_path: Path):
    bundle = make_bundle(tmp_path)
    gaia = StubGaia()
    brief = build_brief(bundle, gaia)  # type: ignore[arg-type]
    # config hints first, then gaia terms + aliases, deduplicated
    assert brief.vocab_hints == ["Kubernetes", "SSO", "SCIM", "scim"]
    names = [p.name for p in brief.participants]
    assert names == ["岡村", "田中太郎"]
    tanaka = brief.participants[1]
    assert tanaka.aliases == ["Tanaka"] and tanaka.note == "acme 情シス"
    assert brief.previous_points == ["前回はリリース日を 9/10 に決めた"]
    assert brief.background == ["acme は SSO 移行の途中"]
    assert [(s.system, s.uri, s.note) for s in brief.sources] == [
        ("notion", "notion://page/prev", "前回議事録"),
        ("box", "box://file/9", None),
    ]
    assert ("get_glossary", "acme") in gaia.calls
    assert ("search_context", "定例ミーティング", "acme") in gaia.calls
    assert bundle.manifest.artifacts[BRIEF_ARTIFACT_KEY].params == {"gaia": True, "version": 1}


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
