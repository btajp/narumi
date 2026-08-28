"""Finite hierarchical summaries reuse successful prompts and reject non-shrinking output."""

from collections import Counter
from pathlib import Path

import pytest
from narumi.bundle import Bundle
from narumi.errors import EngineUnavailableError
from narumi.generate.bounded import MinutesLimits, bounded_minutes, split_text
from narumi.generate.checkpoints import MinutesCheckpoints
from narumi.generate.prompts import render_prompt
from narumi.llm.base import CapabilityProfile


class SummaryProvider:
    name = "codex-fixture"
    profile = CapabilityProfile(False, 0, "subscription", "openai", False)

    def __init__(self, *, fail_reduce=False, shrink=True):
        self.calls = []
        self.fail_reduce = fail_reduce
        self.shrink = shrink
        self.reductions = 0
        self.failed_prompt = None

    def complete(self, prompt, *, system=None, max_tokens=None):
        self.calls.append((prompt, system))
        if not self.shrink:
            return "- 決定事項を残します。\n" * 120
        if "元の半分以下" in prompt:
            self.reductions += 1
            if self.fail_reduce and self.reductions == 2:
                self.failed_prompt = prompt
                raise EngineUnavailableError("fixture known failure")
            return "- 期限は月曜。\n" * 5
        if "<transcript>" in prompt:
            return "- 期限は月曜。\n" * 50
        return "## 決定事項\n- 期限は月曜。\n"


def checkpoint_provider(tmp_path, backend, limits):
    bundle = Bundle.create(tmp_path / "meetings", meeting_name="長文fixture")

    def wrapper():
        return MinutesCheckpoints(
            bundle, backend, inputs={}, params={"limits": limits.params()}, limits=limits
        )

    return wrapper


def generate(provider, limits, *, length=7_000):
    return bounded_minutes(
        ["会議の発言。" * (length // 6)],
        meeting_name="長文fixture",
        provider=provider,
        brief="",
        system="書記です。",
        limits=limits,
    )


def test_long_meeting_reduces_within_budget_and_reuses_successful_prompts(tmp_path):
    limits = MinutesLimits(input_chars=1_100)
    backend = SummaryProvider(fail_reduce=True)
    wrapper = checkpoint_provider(tmp_path, backend, limits)
    with pytest.raises(EngineUnavailableError, match="fixture known failure"):
        generate(wrapper(), limits)
    answer, chunks = generate(wrapper(), limits)
    assert chunks > 3 and "期限は月曜" in answer
    assert backend.reductions > 2
    calls = Counter(prompt for prompt, _ in backend.calls)
    assert all(
        count == (2 if prompt == backend.failed_prompt else 1) for prompt, count in calls.items()
    )
    assert all(len(prompt) + len(system) <= limits.input_chars for prompt, system in backend.calls)
    assert len(backend.calls) <= limits.max_requests
    before = len(backend.calls)
    assert generate(wrapper(), limits) == (answer, chunks)
    assert len(backend.calls) == before


def test_nonshrinking_model_stops_without_repeated_send_on_retry(tmp_path):
    limits = MinutesLimits(input_chars=1_100)
    backend = SummaryProvider(shrink=False)
    wrapper = checkpoint_provider(tmp_path, backend, limits)
    with pytest.raises(EngineUnavailableError, match="did not shrink"):
        generate(wrapper(), limits, length=1_000)
    before = len(backend.calls)
    assert before < limits.max_requests
    with pytest.raises(EngineUnavailableError, match="did not shrink"):
        generate(wrapper(), limits, length=1_000)
    assert len(backend.calls) == before


def test_request_budget_rejects_oversized_meeting_before_sending(tmp_path):
    limits = MinutesLimits(input_chars=1_100, max_requests=3)
    backend = SummaryProvider()
    wrapper = checkpoint_provider(tmp_path, backend, limits)
    with pytest.raises(EngineUnavailableError, match="request budget"):
        generate(wrapper(), limits)
    assert backend.calls == []
    # Long utterances are split without silently discarding characters or exceeding the cap.
    text = "発言。" * 999 + "\n次の発言。"
    parts = split_text(text, 123)
    assert "".join(parts) == text and max(map(len, parts)) <= 123


def test_reduce_prompt_snapshot():
    rendered = render_prompt(
        "minutes_reduce",
        meeting_name="定例会議",
        brief="",
        summaries="- 月曜までに公開する。",
    )
    snapshot = Path(__file__).parent / "snapshots" / "minutes_reduce.md"
    assert rendered == snapshot.read_text(encoding="utf-8")
