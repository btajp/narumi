"""Claude Agent SDK adapter, gated until authentication and history isolation are verified."""

from __future__ import annotations

import os
from pathlib import Path

from narumi.errors import EngineUnavailableError
from narumi.llm.base import CapabilityProfile

PROVIDER_NAME = "claude-agent-sdk"
ENV_MODEL = "NARUMI_CLAUDE_MODEL"

PROFILE = CapabilityProfile(
    vision=False,
    context_window=200_000,
    cost_class="api",
    data_destination="anthropic",
    tool_use=False,
    max_output_tokens=8192,
)


class ClaudeAgentSDKProvider:
    name = PROVIDER_NAME
    profile = PROFILE

    def __init__(self, *, model: str | None = None) -> None:
        # Preserve legacy model configuration until the stage-selection migration.
        # Do not initialize the SDK or inherit the user's Claude login/configuration.
        self.model = model or os.environ.get(ENV_MODEL) or None

    def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        images: list[Path] | None = None,
        max_tokens: int | None = None,
    ) -> str:
        raise EngineUnavailableError(
            "Claude Agent SDK generation is unavailable until isolated API-key authentication "
            "and disabled history persistence have been verified",
            details={
                "provider": PROVIDER_NAME,
                "reason": "sdk_authentication_and_history_isolation_unverified",
            },
        )
