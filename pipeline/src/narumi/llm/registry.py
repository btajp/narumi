"""Provider registry, static profile lookup and policy-checked selection."""

from __future__ import annotations

import importlib.util
from collections.abc import Callable

from narumi.errors import EngineUnavailableError, NotFoundError
from narumi.llm import anthropic_api, claude_agent, ollama
from narumi.llm.base import CapabilityProfile, LLMProvider
from narumi.llm.fake import FAKE_PROFILE, NONE_PROFILE, FakeProvider, NoneProvider
from narumi.llm.policy import check_policy
from narumi.models import MeetingConfig

PROVIDER_PROFILES: dict[str, CapabilityProfile] = {
    "none": NONE_PROFILE,
    "fake": FAKE_PROFILE,
    claude_agent.PROVIDER_NAME: claude_agent.PROFILE,
    anthropic_api.PROVIDER_NAME: anthropic_api.PROFILE,
    ollama.PROVIDER_NAME: ollama.PROFILE,
}
"""Static profiles: consult these before instantiating anything heavy."""

_FACTORIES: dict[str, Callable[[], LLMProvider]] = {
    "none": NoneProvider,
    "fake": FakeProvider,
    claude_agent.PROVIDER_NAME: claude_agent.ClaudeAgentSDKProvider,
    anthropic_api.PROVIDER_NAME: anthropic_api.AnthropicAPIProvider,
    ollama.PROVIDER_NAME: ollama.OllamaProvider,
}

_IMPORT_REQUIREMENTS: dict[str, str] = {
    claude_agent.PROVIDER_NAME: "claude_agent_sdk",
}

# These providers are usable only through the v6 provider connection/minutes-model path.
# The Anthropic legacy adapter can dispatch a paid request without the connection-scoped
# retry ledger, so the registry must reject it before construction instead of inheriting an
# environment API key.  Keep the name/profile registered so an existing saved value produces
# a deterministic migration error rather than silently falling back to another provider.
_LEGACY_UNAVAILABLE = frozenset({claude_agent.PROVIDER_NAME, anthropic_api.PROVIDER_NAME})
_LEGACY_CONNECTION_REQUIRED = frozenset({anthropic_api.PROVIDER_NAME})
_LEGACY_CONNECTION_REQUIRED_REASON = "legacy_provider_requires_connection_model_selection"


def provider_names() -> list[str]:
    return list(PROVIDER_PROFILES)


def provider_profile(name: str) -> CapabilityProfile:
    """Static capability profile of a registered provider (no instantiation, no network)."""
    try:
        return PROVIDER_PROFILES[name]
    except KeyError:
        raise NotFoundError(
            f"unknown llm_provider: {name}", details={"available": provider_names()}
        ) from None


def available_providers() -> list[str]:
    """Legacy providers that can actually complete a request in this process."""
    names: list[str] = []
    for name in PROVIDER_PROFILES:
        if name in _LEGACY_UNAVAILABLE:
            continue
        module = _IMPORT_REQUIREMENTS.get(name)
        if module is None or importlib.util.find_spec(module) is not None:
            names.append(name)
    return names


def get_provider(name: str) -> LLMProvider:
    """Instantiate a provider by registry name (``NotFoundError`` for unknown names)."""
    provider_profile(name)
    _reject_legacy_connection_required(name)
    return _FACTORIES[name]()


def select_provider(config: MeetingConfig) -> LLMProvider:
    """Policy-checked selection: profile check first (cheap), instantiate only if allowed."""
    profile = provider_profile(config.llm_provider)
    _reject_legacy_connection_required(config.llm_provider)
    check_policy(
        profile,
        config.external_send_policy,
        provider=config.llm_provider,
    )
    return get_provider(config.llm_provider)


def _reject_legacy_connection_required(name: str) -> None:
    if name not in _LEGACY_CONNECTION_REQUIRED:
        return
    raise EngineUnavailableError(
        "Legacy llm_provider=anthropic-api is unavailable; save an Anthropic API "
        "provider connection and select its minutes model",
        details={
            "provider": name,
            "reason": _LEGACY_CONNECTION_REQUIRED_REASON,
        },
    )
