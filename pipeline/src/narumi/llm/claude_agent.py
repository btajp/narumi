"""``claude-agent-sdk`` provider: Claude Code subscription via the Claude Agent SDK."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from narumi.errors import EngineUnavailableError, InvalidArgumentError
from narumi.llm.base import CapabilityProfile

PROVIDER_NAME = "claude-agent-sdk"
ENV_MODEL = "NARUMI_CLAUDE_MODEL"
"""Optional model override passed to ``ClaudeAgentOptions(model=…)``; default = CLI default."""

PROFILE = CapabilityProfile(
    vision=True,
    context_window=200_000,
    cost_class="subscription",
    data_destination="anthropic",
    tool_use=True,
    max_output_tokens=8192,
)


def _import_sdk():  # noqa: ANN202 - module object, typed via attribute access below
    try:
        import claude_agent_sdk
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise EngineUnavailableError(
            "claude-agent-sdk is not installed (uv sync --extra claude)",
            details={"provider": PROVIDER_NAME, "error": str(exc)},
        ) from exc
    return claude_agent_sdk


class ClaudeAgentSDKProvider:
    name = PROVIDER_NAME
    profile = PROFILE

    def __init__(self, *, model: str | None = None) -> None:
        _import_sdk()
        self.model = model or os.environ.get(ENV_MODEL) or None

    def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        images: list[Path] | None = None,
        max_tokens: int | None = None,
    ) -> str:
        if images:
            # The installed SDK documents string / text-dict prompts only; do not guess the wire
            # format for image blocks.
            raise InvalidArgumentError(
                "claude-agent-sdk provider does not accept image inputs in this version",
                details={"provider": PROVIDER_NAME, "images": [str(p) for p in images]},
            )
        try:
            return asyncio.run(self._collect(prompt, system))
        except RuntimeError as exc:
            raise EngineUnavailableError(
                "claude-agent-sdk provider must be called outside a running event loop",
                details={"provider": PROVIDER_NAME, "error": str(exc)},
            ) from exc

    async def _collect(self, prompt: str, system: str | None) -> str:
        sdk = _import_sdk()
        options = sdk.ClaudeAgentOptions(
            system_prompt=system,
            max_turns=1,
            tools=[],
            allowed_tools=[],
            permission_mode="dontAsk",
            model=self.model,
        )
        chunks: list[str] = []
        try:
            async for message in sdk.query(prompt=prompt, options=options):
                if isinstance(message, sdk.AssistantMessage):
                    if message.error is not None:
                        raise EngineUnavailableError(
                            f"claude-agent-sdk answered with error {message.error}",
                            details={"provider": PROVIDER_NAME, "error": message.error},
                        )
                    for block in message.content:
                        if isinstance(block, sdk.TextBlock):
                            chunks.append(block.text)
                elif isinstance(message, sdk.ResultMessage) and message.is_error:
                    raise EngineUnavailableError(
                        f"claude-agent-sdk run failed: {message.subtype}",
                        details={"provider": PROVIDER_NAME, "result": message.result},
                    )
        except sdk.ClaudeSDKError as exc:
            raise EngineUnavailableError(
                f"claude-agent-sdk is unavailable: {exc}",
                details={"provider": PROVIDER_NAME, "error": type(exc).__name__},
            ) from exc
        text = "".join(chunks).strip()
        if not text:
            raise EngineUnavailableError(
                "claude-agent-sdk returned no text", details={"provider": PROVIDER_NAME}
            )
        return text
