"""Explicit, non-secret selection of a model for text minutes generation."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

ReasoningEffort = Annotated[
    str, Field(min_length=1, max_length=32, pattern=r"^[a-z][a-z0-9_-]{0,31}$")
]


class ModelSelection(BaseModel):
    """Pinned connection and model; capabilities are validated by the server."""

    model_config = ConfigDict(extra="forbid", strict=True)

    provider: Literal["codex-app-server"]
    connection_id: str = Field(pattern=r"^conn-[0-9a-f]{12,32}$")
    connection_revision: int = Field(ge=1)
    model_id: str = Field(min_length=1, max_length=256)
    parameters: dict[Literal["reasoning_effort"], ReasoningEffort] = Field(default_factory=dict)
    cache_epoch: int = Field(default=0, ge=0)
