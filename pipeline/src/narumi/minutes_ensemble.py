"""Explicit text-minutes ensemble selection and confirmed-call retry shapes."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from narumi.model_selection import ModelSelection

GeneratorId = Annotated[
    str,
    StringConstraints(
        min_length=36,
        max_length=36,
        pattern=r"^gen-[0-9a-f]{32}$",
    ),
]
GeneratorLabel = Annotated[
    str,
    StringConstraints(min_length=1, max_length=80, pattern=r"\S"),
]
RunId = Annotated[
    str,
    StringConstraints(min_length=36, max_length=36, pattern=r"^run-[0-9a-f]{32}$"),
]
NodeId = Annotated[
    str,
    StringConstraints(min_length=69, max_length=69, pattern=r"^node-[0-9a-f]{64}$"),
]
CallId = Annotated[
    str,
    StringConstraints(min_length=69, max_length=69, pattern=r"^call-[0-9a-f]{64}$"),
]
AttemptId = Annotated[
    str,
    StringConstraints(min_length=40, max_length=40, pattern=r"^attempt-[0-9a-f]{32}$"),
]


class MinutesEnsembleGenerator(BaseModel):
    """One UI/provenance generator mapped to a pinned text model."""

    model_config = ConfigDict(extra="forbid", strict=True)

    id: GeneratorId
    label: GeneratorLabel
    selection: ModelSelection


class MinutesEnsembleSelection(BaseModel):
    """Two to four generators and one explicit synthesizer."""

    model_config = ConfigDict(extra="forbid", strict=True)

    generators: list[MinutesEnsembleGenerator] = Field(min_length=2, max_length=4)
    synthesizer: ModelSelection

    @model_validator(mode="after")
    def _unique_generator_ids(self) -> MinutesEnsembleSelection:
        ids = [generator.id for generator in self.generators]
        if len(ids) != len(set(ids)):
            raise ValueError("The minutes ensemble generator IDs must be unique")
        return self


class MinutesRetry(BaseModel):
    """One explicit confirmation for a blocked minutes-generation call."""

    model_config = ConfigDict(extra="forbid", strict=True)

    run_id: RunId
    node_id: NodeId
    call_id: CallId
    blocked_attempt_id: AttemptId
