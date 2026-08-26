"""Providers that never leave the machine: ``none`` (disabled) and ``fake`` (deterministic)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from narumi.errors import InvalidArgumentError
from narumi.llm.base import CapabilityProfile

NONE_PROFILE = CapabilityProfile(
    vision=False,
    context_window=0,
    cost_class="local",
    data_destination="local",
    tool_use=False,
    max_output_tokens=0,
)

FAKE_PROFILE = CapabilityProfile(
    vision=False,
    context_window=8000,
    cost_class="local",
    data_destination="local",
    tool_use=False,
    max_output_tokens=1024,
)

FAKE_EXCERPT_CHARS = 60
_DATA_BLOCK_RE = re.compile(r"<(\w+)>\n?(.*?)</\1>", re.DOTALL)


class NoneProvider:
    """Placeholder for ``llm_provider = none``: selecting it is fine, calling it is an error."""

    name = "none"
    profile = NONE_PROFILE

    def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        images: list[Path] | None = None,
        max_tokens: int | None = None,
    ) -> str:
        raise InvalidArgumentError(
            "llm_provider is none: set llm_provider in the meeting config to use an LLM"
        )


@dataclass(frozen=True)
class FakeCall:
    prompt: str
    system: str | None
    images: tuple[Path, ...]
    max_tokens: int | None


@dataclass
class FakeProvider:
    """Deterministic stand-in for snapshot tests.

    The answer echoes every ``## `` section header found in the *instruction* part of the prompt
    (data blocks such as ``<transcript>…</transcript>`` are ignored) followed by a bullet quoting
    the first 60 characters of the first data block. Without headers it returns the bare excerpt.
    """

    name: str = "fake"
    profile: CapabilityProfile = FAKE_PROFILE
    calls: list[FakeCall] = field(default_factory=list)

    def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        images: list[Path] | None = None,
        max_tokens: int | None = None,
    ) -> str:
        self.calls.append(FakeCall(prompt, system, tuple(images or ()), max_tokens))
        blocks = _DATA_BLOCK_RE.findall(prompt)
        instructions = _DATA_BLOCK_RE.sub("", prompt)
        headers: list[str] = []
        for line in instructions.splitlines():
            if line.startswith("## ") and line.strip() not in headers:
                headers.append(line.strip())
        excerpt = _excerpt(blocks[0][1] if blocks else prompt)
        if not headers:
            return f"（fake）{excerpt}"
        return "".join(f"{header}\n- （fake）{excerpt}\n" for header in headers)


def _excerpt(text: str) -> str:
    collapsed = " ".join(text.split())
    return collapsed[:FAKE_EXCERPT_CHARS]
