"""Explicit, non-secret selection of a model for text minutes generation."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

ReasoningEffort = Annotated[
    str, Field(min_length=1, max_length=32, pattern=r"^[a-z][a-z0-9_-]{0,31}$")
]
MinutesProvider = Literal[
    "codex-app-server",
    "claude-agent-sdk",
    "openai-api",
    "openai-compatible-api",
    "anthropic-api",
    "ollama",
]
MinutesMaxTokens = Annotated[int, Field(ge=1, le=32768)]
MINUTES_PARAMETER_NAMES = {
    "codex-app-server": frozenset({"reasoning_effort"}),
    "claude-agent-sdk": frozenset(),
    "openai-api": frozenset({"reasoning_effort", "max_tokens"}),
    "openai-compatible-api": frozenset({"max_tokens"}),
    "anthropic-api": frozenset({"max_tokens"}),
    "ollama": frozenset({"max_tokens"}),
}


class ModelSelection(BaseModel):
    """Pinned connection and model; capabilities are validated by the server."""

    model_config = ConfigDict(extra="forbid", strict=True)

    provider: MinutesProvider
    connection_id: str = Field(pattern=r"^conn-[0-9a-f]{12,32}$")
    connection_revision: int = Field(ge=1)
    model_id: str = Field(min_length=1, max_length=256)
    parameters: dict[
        Literal["reasoning_effort", "max_tokens"], ReasoningEffort | MinutesMaxTokens
    ] = Field(default_factory=dict)
    cache_epoch: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _provider_parameters(self) -> ModelSelection:
        if set(self.parameters) - MINUTES_PARAMETER_NAMES[self.provider]:
            raise ValueError("The minutes parameters are not supported by this provider")
        if (
            "reasoning_effort" in self.parameters
            and type(self.parameters["reasoning_effort"]) is not str
        ):
            raise ValueError("The reasoning effort must be a string")
        if "max_tokens" in self.parameters and type(self.parameters["max_tokens"]) is not int:
            raise ValueError("The output token limit must be an integer")
        return self
