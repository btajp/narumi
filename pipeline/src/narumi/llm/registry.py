"""Provider registry, static profile lookup and policy-checked selection."""

from __future__ import annotations

import importlib.util
from collections.abc import Callable

from narumi.errors import NotFoundError
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
    anthropic_api.PROVIDER_NAME: "anthropic",
}


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
    """Registered names whose Python dependencies are importable (``none`` / ``fake`` always)."""
    names: list[str] = []
    for name in PROVIDER_PROFILES:
        module = _IMPORT_REQUIREMENTS.get(name)
        if module is None or importlib.util.find_spec(module) is not None:
            names.append(name)
    return names


def get_provider(name: str) -> LLMProvider:
    """Instantiate a provider by registry name (``NotFoundError`` for unknown names)."""
    provider_profile(name)
    return _FACTORIES[name]()


def select_provider(config: MeetingConfig) -> LLMProvider:
    """Policy-checked selection: profile check first (cheap), instantiate only if allowed."""
    check_policy(
        provider_profile(config.llm_provider),
        config.external_send_policy,
        provider=config.llm_provider,
    )
    return get_provider(config.llm_provider)
