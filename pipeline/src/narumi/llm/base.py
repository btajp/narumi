"""LLM provider abstraction: capability profile + minimal completion protocol."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, runtime_checkable

CostClass = Literal["local", "subscription", "api"]


@dataclass(frozen=True)
class CapabilityProfile:
    """What a provider can do and where the data goes (basis of ``external_send_policy``)."""

    vision: bool
    context_window: int
    cost_class: CostClass
    data_destination: str
    """``"local"`` when nothing leaves the machine; otherwise the vendor (``anthropic`` …)."""
    tool_use: bool
    max_output_tokens: int = 4096


@runtime_checkable
class LLMProvider(Protocol):
    name: str
    profile: CapabilityProfile

    def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        images: list[Path] | None = None,
        max_tokens: int | None = None,
    ) -> str:
        """Return the model's text answer for one prompt (single turn, no tools)."""
        ...
