"""Bounded text summarization for connection-selected minutes models.

Character budgets are local adapter limits, not claims about undocumented model capacity.
Unknown context sizes use half the adapter's maximum input, including prompt instructions and
the meeting brief. A finite request budget and shrinking reductions bound subscription use.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from narumi.errors import EngineUnavailableError
from narumi.generate.prompts import render_prompt
from narumi.llm.base import LLMProvider

MAX_INPUT_CHARS = 24_000
UNKNOWN_INPUT_CHARS = 12_000
MIN_INPUT_CHARS = 1_000
MAX_REQUESTS = 64
MAX_REDUCTIONS = 6


@dataclass(frozen=True)
class MinutesLimits:
    input_chars: int
    max_requests: int = MAX_REQUESTS
    max_reductions: int = MAX_REDUCTIONS

    @classmethod
    def for_provider(cls, provider: LLMProvider) -> MinutesLimits:
        context = provider.profile.context_window
        budget = (
            min(MAX_INPUT_CHARS, max(MIN_INPUT_CHARS, context // 2))
            if context > 0
            else UNKNOWN_INPUT_CHARS
        )
        return cls(budget)

    def params(self) -> dict[str, Any]:
        return {"bounded_prompt_version": "minutes-reduce-v1", **asdict(self)}


def bounded_minutes(
    lines: list[str],
    *,
    meeting_name: str,
    provider: LLMProvider,
    brief: str,
    system: str,
    limits: MinutesLimits,
) -> tuple[str, int]:
    def chunk_prompt(text: str, index: int, total: int) -> str:
        return render_prompt(
            "minutes_chunk",
            meeting_name=meeting_name,
            index=index,
            total=total,
            brief=brief,
            transcript=text,
        )

    # Maximum index width reserves template space before partitioning the transcript.
    empty_chunk = chunk_prompt("", limits.max_requests, limits.max_requests)
    chunks = split_text("\n".join(lines), _available(limits, system, empty_chunk)) or [""]
    if len(chunks) + 1 > limits.max_requests:
        raise _limit_error("The transcript exceeds this minutes attempt's request budget")
    summaries = [
        provider.complete(chunk_prompt(text, index, len(chunks)), system=system).strip()
        for index, text in enumerate(chunks, start=1)
    ]
    for depth in range(limits.max_reductions + 1):
        notes = _notes(summaries)
        final_prompt = render_prompt(
            "minutes_final",
            meeting_name=meeting_name,
            total=len(chunks),
            brief=brief,
            summaries=notes,
        )
        if len(final_prompt) + len(system) <= limits.input_chars:
            return provider.complete(final_prompt, system=system), len(chunks)
        if depth == limits.max_reductions:
            raise _limit_error("Minutes summaries exceed the bounded reduction depth")
        empty_reduce = render_prompt(
            "minutes_reduce",
            meeting_name=meeting_name,
            brief=brief,
            summaries="",
        )
        groups = split_text(notes, _available(limits, system, empty_reduce))
        reduced = [
            provider.complete(
                render_prompt(
                    "minutes_reduce",
                    meeting_name=meeting_name,
                    brief=brief,
                    summaries=group,
                ),
                system=system,
            ).strip()
            for group in groups
        ]
        if len(_notes(reduced)) >= len(notes):
            raise _limit_error("The model's minutes summaries did not shrink; reduction stopped")
        summaries = reduced
    raise AssertionError("unreachable bounded reduction")


def split_text(text: str, budget: int) -> list[str]:
    """Prefer line boundaries, but never allow one long utterance to exceed the limit."""
    if budget < 1:
        raise ValueError("The chunk budget must be positive")
    parts: list[str] = []
    pending = ""
    for line in text.splitlines(keepends=True):
        if pending and len(pending) + len(line) > budget:
            parts.append(pending)
            pending = ""
        while len(line) > budget:
            parts.append(line[:budget])
            line = line[budget:]
        pending += line
    if pending:
        parts.append(pending)
    return parts


def _available(limits: MinutesLimits, system: str, empty_prompt: str) -> int:
    available = limits.input_chars - len(system) - len(empty_prompt)
    if available < 128:
        raise _limit_error("The meeting brief and instructions exceed the minutes input budget")
    return available


def _notes(summaries: list[str]) -> str:
    return "\n\n".join(f"### メモ {index}\n{text}" for index, text in enumerate(summaries, start=1))


def _limit_error(message: str) -> EngineUnavailableError:
    return EngineUnavailableError(message, details={"reason": "minutes_generation_limit"})
