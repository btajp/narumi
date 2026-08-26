"""Minutes generation (plain or LLM) and the append-only ``minutes/vN`` stage."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

from narumi.bundle import Bundle, Manifest, MinutesVersionRecord, StageResult, utc_now_iso
from narumi.bundle.hashing import sha256_params
from narumi.generate.integrate import INTEGRATE_KEY, load_merged, uses_llm
from narumi.generate.prompts import render_prompt
from narumi.llm.base import LLMProvider
from narumi.llm.policy import check_policy
from narumi.llm.registry import get_provider, provider_profile
from narumi.models import MeetingConfig, MergedSegment, MergedTranscript, MinutesMeta

MINUTES_PROMPT_VERSION = "minutes-v1"
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
MAX_CHUNK_CHARS = 30_000
MIN_CHUNK_CHARS = 1_000
CHARS_PER_CONTEXT_TOKEN = 1.5
JST = timezone(timedelta(hours=9), "JST")
_HEADER_RE = re.compile(r"^##\s+(.+?)\s*$")


def chunk_budget(provider: LLMProvider) -> int:
    """Characters per chunk derived from the provider's context window (capped at 30k)."""
    return max(
        MIN_CHUNK_CHARS,
        min(MAX_CHUNK_CHARS, int(provider.profile.context_window * CHARS_PER_CONTEXT_TOKEN)),
    )


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
    merged: MergedTranscript, manifest: Manifest, provider: LLMProvider
) -> tuple[dict[str, str], int]:
    lines = transcript_lines(merged)
    chunks = chunk_lines(lines, chunk_budget(provider)) or [[]]
    summaries: list[str] = []
    for i, chunk in enumerate(chunks, start=1):
        prompt = render_prompt(
            CHUNK_PROMPT_NAME,
            meeting_name=manifest.meeting_name,
            index=i,
            total=len(chunks),
            transcript="\n".join(chunk),
        )
        summaries.append(provider.complete(prompt, system=MINUTES_SYSTEM_PROMPT).strip())
    final_prompt = render_prompt(
        FINAL_PROMPT_NAME,
        meeting_name=manifest.meeting_name,
        total=len(chunks),
        summaries="\n\n".join(f"### メモ {i}\n{s}" for i, s in enumerate(summaries, start=1)),
    )
    answer = provider.complete(final_prompt, system=MINUTES_SYSTEM_PROMPT)
    parsed = split_sections(answer)
    sections = {
        name: parsed.get(name) or MISSING_SECTION_TEMPLATE.format(section=name)
        for name in LLM_SECTIONS
    }
    return sections, len(chunks)


def generate_minutes(
    merged: MergedTranscript,
    manifest: Manifest,
    config: MeetingConfig,
    provider: LLMProvider | None,
    *,
    version: int,
    prompt_version: str = MINUTES_PROMPT_VERSION,
) -> tuple[str, MinutesMeta]:
    """Render minutes markdown + metadata. Plain mode when ``provider`` is ``None`` / ``none``."""
    llm = uses_llm(provider)
    provider_name = provider.name if provider is not None else "none"
    generated_at = utc_now_iso()
    if llm:
        assert provider is not None
        sections, chunks = _llm_sections(merged, manifest, provider)
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
            lines.append(f"- **{label}**: {entry.name if entry.name else UNRESOLVED_MARK}")
    else:
        lines.append("- （話者情報なし）")
    for name in LLM_SECTIONS:
        lines += ["", f"## {name}", "", sections[name]]
    lines += ["", "## 文字起こし（全文）", ""]
    lines += transcript_lines(merged) or ["- （発話なし）"]
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
        },
        unresolved_speakers=unresolved,
    )
    return markdown, meta


def minutes_key(version: int) -> str:
    return f"minutes/v{version}"


def minutes_output(version: int) -> str:
    return f"minutes/v{version}/minutes.md"


def run_generate(bundle: Bundle, *, force: bool = False) -> StageResult:
    """Append a new minutes version unless the latest one has identical inputs / params.

    Existing versions are never overwritten: a changed input or ``force`` always creates
    ``v(latest+1)``. The manifest's ``minutes_versions`` is appended and saved.
    """
    config = bundle.manifest.config
    inputs = {INTEGRATE_KEY: bundle.artifact_hash(INTEGRATE_KEY)}
    name = config.llm_provider
    check_policy(provider_profile(name), config.external_send_policy, provider=name)
    params = {
        "provider": name,
        "prompt_version": MINUTES_PROMPT_VERSION,
        "language": config.language,
    }
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
        provider = get_provider(name)
        merged = load_merged(bundle)
        text, meta = generate_minutes(merged, bundle.manifest, config, provider, version=version)
        meta.inputs = dict(inputs)
        meta.params.update(params)
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
