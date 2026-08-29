"""Reviewed OpenAI text capabilities, never inferred from model-name prefixes.

The Models API lists access, not context windows or generation parameters. Only
the exact IDs below can intersect with discovery to become selectable. Recheck
the cited primary sources and increment the version before changing this table.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from types import MappingProxyType

from narumi.errors import InvalidArgumentError, ModelUnavailableError
from narumi.providers.metadata.validation import parameter_schema

CAPABILITY_TABLE_VERSION = "openai-text-2026-08-29-v1"
CAPABILITIES_VERIFIED_AT = "2026-08-29"
REASONING_SOURCE_URL = "https://developers.openai.com/api/docs/guides/reasoning"
_MODEL_DOCS = "https://developers.openai.com/api/docs/models/"


@dataclass(frozen=True)
class OpenAIModelCapabilities:
    model_id: str
    display_name: str
    context_window: int
    max_output_tokens: int
    source_url: str
    reasoning_efforts: tuple[str, ...] = ()
    default_reasoning_effort: str | None = None
    reasoning_mode: str | None = None
    reasoning_context: str | None = None
    confirmed_snapshots: tuple[str, ...] = ()
    resolved_revision: str | None = None
    verified_at: str = CAPABILITIES_VERIFIED_AT

    def parameter_schema(self) -> dict:
        schema = parameter_schema(self.max_output_tokens)
        if self.reasoning_efforts:
            schema["properties"]["reasoning_effort"] = {
                "type": "string",
                "enum": list(self.reasoning_efforts),
                "default": self.default_reasoning_effort,
            }
        return schema


_ALIASES = (
    *(
        OpenAIModelCapabilities(
            model_id=f"gpt-5.6-{tier}",
            display_name=f"GPT-5.6 {tier}",
            context_window=1_050_000,
            max_output_tokens=128_000,
            source_url=_MODEL_DOCS + f"gpt-5.6-{tier}",
            reasoning_efforts=("none", "low", "medium", "high", "xhigh", "max"),
            default_reasoning_effort="medium",
            reasoning_mode="standard",
            reasoning_context="current_turn",
        )
        for tier in ("sol", "terra", "luna")
    ),
    OpenAIModelCapabilities(
        model_id="gpt-5.4",
        display_name="GPT-5.4",
        context_window=1_050_000,
        max_output_tokens=128_000,
        source_url=_MODEL_DOCS + "gpt-5.4",
        reasoning_efforts=("none", "low", "medium", "high", "xhigh"),
        default_reasoning_effort="none",
        confirmed_snapshots=("gpt-5.4-2026-03-05",),
    ),
    OpenAIModelCapabilities(
        model_id="gpt-4.1",
        display_name="GPT-4.1",
        context_window=1_047_576,
        max_output_tokens=32_768,
        source_url=_MODEL_DOCS + "gpt-4.1",
        confirmed_snapshots=("gpt-4.1-2025-04-14",),
    ),
    OpenAIModelCapabilities(
        model_id="gpt-4.1-mini",
        display_name="GPT-4.1 mini",
        context_window=1_047_576,
        max_output_tokens=32_768,
        source_url=_MODEL_DOCS + "gpt-4.1-mini",
        confirmed_snapshots=("gpt-4.1-mini-2025-04-14",),
    ),
)

_CAPABILITIES = MappingProxyType(
    {
        entry.model_id: entry
        for alias in _ALIASES
        for entry in (
            alias,
            *(
                replace(
                    alias,
                    model_id=snapshot,
                    display_name=f"{alias.display_name} ({snapshot[-10:]})",
                    confirmed_snapshots=(),
                    resolved_revision=snapshot,
                )
                for snapshot in alias.confirmed_snapshots
            ),
        )
    }
)


def model_capabilities(model_id: str) -> OpenAIModelCapabilities | None:
    """Exact lookup only: unknown aliases, snapshots and fine-tunes stay unknown."""
    return _CAPABILITIES.get(model_id) if isinstance(model_id, str) else None


def confirmed_resolved_model_ids(model_id: str) -> frozenset[str]:
    """A pinned snapshot cannot silently become an alias or another snapshot."""
    capabilities = model_capabilities(model_id)
    if capabilities is None:
        return frozenset()
    return frozenset((model_id, *capabilities.confirmed_snapshots))


def reasoning_payload(model_id: str, effort: str | None) -> dict[str, str] | None:
    """Return only parameters verified for this exact model's API generation."""
    capabilities = model_capabilities(model_id)
    if capabilities is None:
        raise ModelUnavailableError(
            "OpenAI model capabilities are not verified",
            details={"reason": "model_capabilities_unavailable"},
        )
    if not capabilities.reasoning_efforts and effort is None:
        return None
    selected = effort if effort is not None else capabilities.default_reasoning_effort
    if not isinstance(selected, str) or selected not in capabilities.reasoning_efforts:
        raise InvalidArgumentError(
            "OpenAI reasoning effort is not supported by the selected model",
            details={"reason": "unsupported_reasoning_effort"},
        )
    payload = {"effort": selected}
    if capabilities.reasoning_mode is not None:
        payload["mode"] = capabilities.reasoning_mode
    if capabilities.reasoning_context is not None:
        payload["context"] = capabilities.reasoning_context
    return payload
