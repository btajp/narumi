"""Minutes generation (plain or LLM) and the append-only ``minutes/vN`` stage.

The stage embeds the meeting brief (``context/brief.json``) into the LLM prompts (as the
``{{brief}}`` template slot, truncated to a budget scaled from the provider's context window)
and the key slides (``preprocess/slides.json``) into the transcript section, copying the images
to ``minutes/v<N>/slides/`` so the markdown stays self-contained. Both artifacts join the stage
inputs, so a richer brief or new slides append a new version.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from narumi.brief import BRIEF_ARTIFACT_KEY, Brief, inject_brief, load_brief
from narumi.bundle import Bundle, Manifest, MinutesVersionRecord, StageResult, utc_now_iso
from narumi.bundle.hashing import sha256_params
from narumi.errors import AuthenticationRequiredError, InvalidArgumentError
from narumi.generate.bounded import MinutesLimits, bounded_minutes
from narumi.generate.checkpoints import MinutesCheckpoints, check_cancelled
from narumi.generate.integrate import INTEGRATE_KEY, load_merged, uses_llm
from narumi.generate.prompts import render_prompt
from narumi.llm.base import LLMProvider
from narumi.llm.policy import check_policy
from narumi.llm.registry import get_provider, provider_profile
from narumi.models import (
    MeetingConfig,
    MergedSegment,
    MergedTranscript,
    MinutesMeta,
    SpeakerEntry,
)
from narumi.slides.detect import SLIDES_KEY, SlideEntry, load_slides
from narumi.slides.embed import copy_slides_to_minutes, select_slides_for_minutes

if TYPE_CHECKING:
    from narumi.providers.generation import MinutesResolver

MINUTES_PROMPT_VERSION = "minutes-v2"
CHUNK_PROMPT_NAME = "minutes_chunk"
FINAL_PROMPT_NAME = "minutes_final"
MINUTES_SYSTEM_PROMPT = (
    "あなたは日本語の会議議事録を作成する書記です。"
    "事実のみを、指示された見出し構成で簡潔に書いてください。"
)
PRODUCER = ("generate", "1")
LLM_SECTIONS = ("アジェンダ", "議論サマリ", "決定事項", "TODO・宿題")
PLAIN_PLACEHOLDER = "（LLM 未使用のため未生成。llm_provider を設定して regenerate してください）"
MISSING_SECTION_TEMPLATE = "（LLM 応答に「{section}」セクションが含まれていなかったため未生成）"
UNRESOLVED_MARK = "未特定"
UNKNOWN_SPEAKER = "話者不明"
LAYER4_NOTE = "（外部トランスクリプトより）"
"""話者 section marker for names that layer 4 (external transcripts) resolved."""
LAYER3_NOTE = "（画面の話者表示より）"
"""話者 section marker for names that layer 3 (screen vision) suggested."""
MAX_CHUNK_CHARS = 30_000
MIN_CHUNK_CHARS = 1_000
CHARS_PER_CONTEXT_TOKEN = 1.5
MAX_BRIEF_CHARS = 4_000
BRIEF_BUDGET_DIVISOR = 10
"""The brief may take at most 1/10 of the chunk character budget (capped at 4000 chars)."""
JST = timezone(timedelta(hours=9), "JST")
_HEADER_RE = re.compile(r"^##\s+(.+?)\s*$")


def chunk_budget(provider: LLMProvider) -> int:
    """Characters per chunk derived from the provider's context window (capped at 30k)."""
    return max(
        MIN_CHUNK_CHARS,
        min(MAX_CHUNK_CHARS, int(provider.profile.context_window * CHARS_PER_CONTEXT_TOKEN)),
    )


def brief_budget(provider: LLMProvider) -> int:
    """Characters the brief may occupy in a prompt, scaled from the provider's context window."""
    return min(MAX_BRIEF_CHARS, chunk_budget(provider) // BRIEF_BUDGET_DIVISOR)


def brief_block(brief: Brief | None, provider: LLMProvider) -> str:
    """The ``{{brief}}`` template value: a ``<brief>`` data block, or ``""`` when empty.

    Wrapped in a data-block tag so deterministic test providers (and ``split_sections``) never
    mistake the brief's ``## `` headers for instructions; empty briefs render to nothing so
    prompts without context stay byte-identical to the pre-brief era.
    """
    if brief is None:
        return ""
    text = inject_brief(brief, budget_chars=brief_budget(provider))
    if not text:
        return ""
    return f"事前情報（会議ブリーフ。語彙・人名の表記はこれを優先）:\n<brief>\n{text}\n</brief>\n"


def chunk_lines(lines: list[str], budget: int) -> list[list[str]]:
    chunks: list[list[str]] = []
    current: list[str] = []
    size = 0
    for line in lines:
        if current and size + len(line) + 1 > budget:
            chunks.append(current)
            current, size = [], 0
        current.append(line)
        size += len(line) + 1
    if current:
        chunks.append(current)
    return chunks


def format_clock(seconds: float) -> str:
    total = int(seconds)
    return f"{total // 3600:02d}:{total % 3600 // 60:02d}:{total % 60:02d}"


def format_jst(iso_utc: str | None) -> str:
    """Render a UTC ISO timestamp as ``YYYY-MM-DD HH:MM JST`` (``不明`` when absent)."""
    if not iso_utc:
        return "不明"
    try:
        parsed = datetime.fromisoformat(iso_utc.replace("Z", "+00:00"))
    except ValueError:
        return iso_utc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(JST).strftime("%Y-%m-%d %H:%M JST")


def speaker_map_display(entry: SpeakerEntry) -> str:
    """話者 section value: the resolved name (marked when layer 4 / 3 supplied it) or 未特定."""
    if not entry.name:
        return UNRESOLVED_MARK
    if any(evidence.layer == 4 for evidence in entry.evidence):
        return f"{entry.name}{LAYER4_NOTE}"
    if any(evidence.layer == 3 for evidence in entry.evidence):
        return f"{entry.name}{LAYER3_NOTE}"
    return entry.name


def speaker_display(segment: MergedSegment) -> str:
    if segment.speaker_name:
        return segment.speaker_name
    if segment.speaker_label:
        return f"{segment.speaker_label}（{UNRESOLVED_MARK}）"
    return UNKNOWN_SPEAKER


def transcript_lines(merged: MergedTranscript) -> list[str]:
    return [
        f"- [{format_clock(s.start)}] **{speaker_display(s)}**: {s.text}" for s in merged.segments
    ]


def split_sections(text: str) -> dict[str, str]:
    """Split an LLM answer on ``## `` headers → ``{header: body}`` (bodies stripped)."""
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in text.splitlines():
        match = _HEADER_RE.match(line)
        if match:
            current = match.group(1)
            sections.setdefault(current, [])
        elif current is not None:
            sections[current].append(line)
    return {name: "\n".join(body).strip() for name, body in sections.items()}


def _engine_summary(manifest: Manifest, prefix: str, fallback: str) -> str:
    names = sorted(
        {
            f"{rec.producer.name} {rec.producer.version}"
            for key, rec in manifest.artifacts.items()
            if key.startswith(prefix)
        }
    )
    return ", ".join(names) if names else fallback


def _llm_sections(
    merged: MergedTranscript,
    manifest: Manifest,
    provider: LLMProvider,
    brief_text: str,
    limits: MinutesLimits | None = None,
) -> tuple[dict[str, str], int]:
    lines = transcript_lines(merged)
    if limits is not None:
        answer, count = bounded_minutes(
            lines,
            meeting_name=manifest.meeting_name,
            provider=provider,
            brief=brief_text,
            system=MINUTES_SYSTEM_PROMPT,
            limits=limits,
        )
        return _answer_sections(answer), count
    chunks = chunk_lines(lines, chunk_budget(provider)) or [[]]
    summaries: list[str] = []
    for i, chunk in enumerate(chunks, start=1):
        prompt = render_prompt(
            CHUNK_PROMPT_NAME,
            meeting_name=manifest.meeting_name,
            index=i,
            total=len(chunks),
            brief=brief_text,
            transcript="\n".join(chunk),
        )
        summaries.append(provider.complete(prompt, system=MINUTES_SYSTEM_PROMPT).strip())
    final_prompt = render_prompt(
        FINAL_PROMPT_NAME,
        meeting_name=manifest.meeting_name,
        total=len(chunks),
        brief=brief_text,
        summaries="\n\n".join(f"### メモ {i}\n{s}" for i, s in enumerate(summaries, start=1)),
    )
    answer = provider.complete(final_prompt, system=MINUTES_SYSTEM_PROMPT)
    return _answer_sections(answer), len(chunks)


def _answer_sections(answer: str) -> dict[str, str]:
    parsed = split_sections(answer)
    return {
        name: parsed.get(name) or MISSING_SECTION_TEMPLATE.format(section=name)
        for name in LLM_SECTIONS
    }


def generate_minutes(
    merged: MergedTranscript,
    manifest: Manifest,
    config: MeetingConfig,
    provider: LLMProvider | None,
    *,
    version: int,
    prompt_version: str = MINUTES_PROMPT_VERSION,
    brief: Brief | None = None,
    slides: Sequence[SlideEntry] | None = None,
    slide_refs: dict[str, str] | None = None,
    limits: MinutesLimits | None = None,
) -> tuple[str, MinutesMeta]:
    """Render minutes markdown + metadata. Plain mode when ``provider`` is ``None`` / ``none``.

    ``brief`` is injected into the LLM prompts (never into the plain markdown). ``slides`` are
    embedded into the transcript section as image links; ``slide_refs`` maps each slide id to
    its markdown ref relative to the minutes directory (``run_generate`` passes the refs from
    :func:`~narumi.slides.embed.copy_slides_to_minutes`; a slide without a ref is not embedded).
    """
    llm = uses_llm(provider)
    provider_name = provider.name if provider is not None else "none"
    generated_at = utc_now_iso()
    brief_text = ""
    if llm:
        assert provider is not None
        brief_text = brief_block(brief, provider)
        sections, chunks = _llm_sections(merged, manifest, provider, brief_text, limits)
    else:
        sections, chunks = dict.fromkeys(LLM_SECTIONS, PLAIN_PLACEHOLDER), 0

    unresolved = [
        label for label, entry in merged.speaker_map.speakers.items() if entry.name is None
    ]
    transcription = _engine_summary(manifest, "transcripts/own-", config.transcription_engine)
    diarization = _engine_summary(manifest, "diarization/", config.diarization_engine)
    lines: list[str] = [f"# {manifest.meeting_name}", ""]
    lines += [
        "| 項目 | 値 |",
        "| --- | --- |",
        f"| 日時 | {format_jst(manifest.recording.started_at)} |",
        f"| 会議 ID | {manifest.meeting_id} |",
        f"| 議事録バージョン | v{version} |",
        f"| 生成日時 | {format_jst(generated_at)} |",
        f"| 文字起こしエンジン | {transcription} |",
        f"| 話者分離エンジン | {diarization} |",
        f"| LLM プロバイダ | {provider_name} |",
        f"| 外部送信ポリシー | {config.external_send_policy.value} |",
        "",
        "## 話者",
        "",
    ]
    if merged.speaker_map.speakers:
        for label, entry in merged.speaker_map.speakers.items():
            lines.append(f"- **{label}**: {speaker_map_display(entry)}")
    else:
        lines.append("- （話者情報なし）")
    for name in LLM_SECTIONS:
        lines += ["", f"## {name}", "", sections[name]]
    lines += ["", "## 文字起こし（全文）", ""]
    embedded = transcript_lines_with_slides(merged, slides or (), slide_refs or {})
    lines += embedded or ["- （発話なし）"]
    markdown = "\n".join(lines) + "\n"

    meta = MinutesMeta(
        version=version,
        generated_at=generated_at,
        provider=provider_name,
        prompt_version=prompt_version if llm else None,
        params={
            "language": config.language,
            "mode": "llm" if llm else "plain",
            "chunks": chunks,
            "integration": merged.params.get("integration"),
            "slides": len([s for s in slides or () if s.id in (slide_refs or {})]),
            "brief": bool(brief_text),
        },
        unresolved_speakers=unresolved,
    )
    return markdown, meta


def transcript_lines_with_slides(
    merged: MergedTranscript,
    slides: Sequence[SlideEntry],
    slide_refs: dict[str, str],
) -> list[str]:
    """Transcript bullets with each key slide's image inserted after its anchor segment.

    Anchoring comes from :func:`~narumi.slides.embed.select_slides_for_minutes`; a slide anchored
    to ``None`` (before every segment) is placed right under the section header. Slides missing
    from ``slide_refs`` are skipped. Without slides this is exactly :func:`transcript_lines`.
    """
    anchored = select_slides_for_minutes(slides, merged.segments) if slides else []
    by_anchor: dict[str | None, list[str]] = {}
    for slide, anchor in anchored:
        ref = slide_refs.get(slide.id)
        if ref is None:
            continue
        by_anchor.setdefault(anchor, []).append(
            f"![{slide.id} ({format_clock(slide.start)})]({ref})"
        )
    if not by_anchor:
        return transcript_lines(merged)
    lines: list[str] = []
    head = by_anchor.get(None)
    if head:
        lines += [*head, ""]
    for segment in merged.segments:
        lines.append(
            f"- [{format_clock(segment.start)}] **{speaker_display(segment)}**: {segment.text}"
        )
        images = by_anchor.get(segment.id)
        if images:
            lines += ["", *images, ""]
    return lines


def minutes_key(version: int) -> str:
    return f"minutes/v{version}"


def minutes_output(version: int) -> str:
    return f"minutes/v{version}/minutes.md"


def run_generate(
    bundle: Bundle,
    *,
    force: bool = False,
    minutes_resolver: MinutesResolver | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> StageResult:
    """Append a new minutes version unless the latest one has identical inputs / params.

    Existing versions are never overwritten: a changed input or ``force`` always creates
    ``v(latest+1)``. The manifest's ``minutes_versions`` is appended and saved.

    The key-slide index and the meeting brief join the inputs when their stages ran, so new
    slides or a richer brief produce a new version; their content is embedded by
    :func:`generate_minutes` (slides into the transcript, the brief into the LLM prompts).
    """
    config = bundle.manifest.config
    if config.minutes_model is not None and force:
        raise InvalidArgumentError(
            "Codex minutes cannot use force; start a new cache epoch instead"
        )
    check_cancelled(should_cancel)
    selected_provider = None
    limits = None
    if config.minutes_model is not None:
        if minutes_resolver is None:
            raise AuthenticationRequiredError(
                "Connected minutes generation requires the authenticated resident server"
            )
        selected_provider = minutes_resolver.resolve(config, should_cancel=should_cancel)
        limits = MinutesLimits.for_provider(selected_provider)
        name = selected_provider.name
    else:
        name = config.llm_provider
        check_policy(provider_profile(name), config.external_send_policy, provider=name)
    inputs = {INTEGRATE_KEY: bundle.artifact_hash(INTEGRATE_KEY)}
    for extra_key in (SLIDES_KEY, BRIEF_ARTIFACT_KEY):
        record = bundle.artifact(extra_key)
        if record is not None:
            inputs[extra_key] = record.sha256
    params = {
        "provider": name,
        "prompt_version": MINUTES_PROMPT_VERSION,
        "language": config.language,
    }
    if selected_provider is not None:
        params.update(selected_provider.generation_params)
        params["generation_limits"] = limits.params()
    latest = bundle.manifest.latest_minutes_version
    if latest is not None and not force:
        existing = bundle.artifact(minutes_key(latest))
        if (
            existing is not None
            and existing.inputs == inputs
            and existing.params_hash == sha256_params(params)
            and bundle.abspath(existing.path).exists()
        ):
            return StageResult(
                key=minutes_key(latest),
                path=bundle.abspath(existing.path),
                record=existing,
                skipped=True,
            )

    version = bundle.next_minutes_version()
    output = minutes_output(version)

    def produce(out: Path) -> None:
        provider = selected_provider if selected_provider is not None else get_provider(name)
        if selected_provider is not None:
            provider = MinutesCheckpoints(
                bundle,
                provider,
                inputs=inputs,
                params=params,
                limits=limits,
                should_cancel=should_cancel,
            )
        merged = load_merged(bundle)
        slides = load_slides(bundle) if bundle.artifact(SLIDES_KEY) is not None else []
        refs = copy_slides_to_minutes(bundle, version, slides) if slides else {}
        text, meta = generate_minutes(
            merged,
            bundle.manifest,
            config,
            provider,
            version=version,
            brief=load_brief(bundle),
            slides=slides or None,
            slide_refs=refs or None,
            limits=limits,
        )
        meta.inputs = dict(inputs)
        meta.params.update(params)
        check_cancelled(should_cancel)
        out.write_text(text, encoding="utf-8")
        bundle.write_json(f"minutes/v{version}/meta.json", meta)

    result = bundle.run_stage(
        minutes_key(version),
        inputs=inputs,
        params=params,
        producer=PRODUCER,
        output=output,
        fn=produce,
        force=force,
    )
    bundle.manifest.minutes_versions.append(
        MinutesVersionRecord(
            version=version,
            path=output,
            generated_at=result.record.created_at,
            provider=name,
        )
    )
    bundle.save()
    return result
