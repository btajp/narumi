"""Preflight and bind every text-minutes ensemble selection as one provider snapshot."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError as ModelValidationError

from narumi.errors import InvalidArgumentError
from narumi.minutes_ensemble import MinutesEnsembleSelection
from narumi.models import ExternalSendPolicy, MeetingConfig
from narumi.providers.generation import CancelCheck, MinutesResolver
from narumi.providers.minutes_selection import (
    BoundMinutesSelection,
    MinutesSelectionInspection,
)

if TYPE_CHECKING:
    from narumi.providers.service import ProviderService


@dataclass(frozen=True)
class EnsembleGeneratorInspection:
    generator_id: str
    label: str
    display_order: int
    selection: MinutesSelectionInspection


@dataclass(frozen=True)
class CanonicalGeneratorProvenance:
    """UI identity mapped to one semantic generator group without affecting its key."""

    generator_id: str
    label: str
    display_order: int
    duplicate_ordinal: int


@dataclass(frozen=True)
class CanonicalGeneratorInspection:
    """One canonical slot; equal semantic keys share the first slot's model call."""

    canonical_ordinal: int
    duplicate_ordinal: int
    shared_call_canonical_ordinal: int
    semantic_key: tuple[str, int]
    selection: MinutesSelectionInspection
    provenance: CanonicalGeneratorProvenance

    @property
    def owns_shared_call(self) -> bool:
        return self.canonical_ordinal == self.shared_call_canonical_ordinal


@dataclass(frozen=True)
class EnsembleInspection:
    """All selections verified from one provider-store transaction."""

    generators: tuple[EnsembleGeneratorInspection, ...]
    canonical_generators: tuple[CanonicalGeneratorInspection, ...]
    synthesizer: MinutesSelectionInspection

    def generator(self, generator_id: str) -> EnsembleGeneratorInspection:
        matches = [item for item in self.generators if item.generator_id == generator_id]
        if len(matches) != 1:
            raise InvalidArgumentError("The ensemble generator is not configured")
        return matches[0]


@dataclass(frozen=True)
class ResolvedEnsembleGenerator:
    generator_id: str
    label: str
    display_order: int
    binding: BoundMinutesSelection


@dataclass(frozen=True)
class CanonicalResolvedGenerator:
    """One canonical slot whose binding is shared across an equal semantic key."""

    canonical_ordinal: int
    duplicate_ordinal: int
    shared_call_canonical_ordinal: int
    semantic_key: tuple[str, int]
    binding: BoundMinutesSelection
    provenance: CanonicalGeneratorProvenance

    @property
    def owns_shared_call(self) -> bool:
        return self.canonical_ordinal == self.shared_call_canonical_ordinal


@dataclass(frozen=True)
class ResolvedEnsemble:
    """Display-order bindings plus a semantic execution view; no generation has started."""

    inspection: EnsembleInspection
    generators: tuple[ResolvedEnsembleGenerator, ...]
    canonical_generators: tuple[CanonicalResolvedGenerator, ...]
    synthesizer: BoundMinutesSelection

    def generator(self, generator_id: str) -> ResolvedEnsembleGenerator:
        matches = [item for item in self.generators if item.generator_id == generator_id]
        if len(matches) != 1:
            raise InvalidArgumentError("The ensemble generator is not configured")
        return matches[0]

    @property
    def call_generators(self) -> tuple[CanonicalResolvedGenerator, ...]:
        """Return exactly one binding per semantic key, in canonical order."""
        return tuple(item for item in self.canonical_generators if item.owns_shared_call)


class EnsembleResolver:
    """Validate every required model before any generator can be called."""

    def __init__(self, service: ProviderService) -> None:
        self.minutes = MinutesResolver(service)

    def validate_in_transaction(
        self, config: MeetingConfig, document: dict[str, Any]
    ) -> EnsembleInspection:
        ensemble, policy = self._validated_config(config)
        generators = tuple(
            EnsembleGeneratorInspection(
                generator_id=generator.id,
                label=generator.label,
                display_order=index,
                selection=self.minutes.validate_selection_in_transaction(
                    generator.selection,
                    policy,
                    document,
                ),
            )
            for index, generator in enumerate(ensemble.generators)
        )
        synthesizer = self.minutes.validate_selection_in_transaction(
            ensemble.synthesizer,
            policy,
            document,
        )
        return EnsembleInspection(
            generators=generators,
            canonical_generators=_canonical_inspections(generators),
            synthesizer=synthesizer,
        )

    def resolve(
        self, config: MeetingConfig, *, should_cancel: CancelCheck | None = None
    ) -> ResolvedEnsemble:
        ensemble, policy = self._validated_config(config)
        with self.minutes.service.store.transaction() as document:
            bound = tuple(
                ResolvedEnsembleGenerator(
                    generator_id=generator.id,
                    label=generator.label,
                    display_order=index,
                    binding=self.minutes._resolve_selection_in_transaction(
                        generator.selection,
                        policy,
                        document,
                        should_cancel=should_cancel,
                    ),
                )
                for index, generator in enumerate(ensemble.generators)
            )
            synthesizer = self.minutes._resolve_selection_in_transaction(
                ensemble.synthesizer,
                policy,
                document,
                should_cancel=should_cancel,
            )
        display_inspections = tuple(
            EnsembleGeneratorInspection(
                generator_id=item.generator_id,
                label=item.label,
                display_order=item.display_order,
                selection=item.binding.inspection,
            )
            for item in bound
        )
        inspection = EnsembleInspection(
            generators=display_inspections,
            canonical_generators=_canonical_inspections(display_inspections),
            synthesizer=synthesizer.inspection,
        )
        return ResolvedEnsemble(
            inspection=inspection,
            generators=bound,
            canonical_generators=_canonical_bindings(bound),
            synthesizer=synthesizer,
        )

    @staticmethod
    def _validated_config(
        config: MeetingConfig,
    ) -> tuple[MinutesEnsembleSelection, ExternalSendPolicy]:
        try:
            snapshot = MeetingConfig.model_validate(config.model_dump(warnings=False))
        except (ModelValidationError, AttributeError):
            raise InvalidArgumentError("The minutes ensemble selection is invalid") from None
        if snapshot.minutes_model is not None or snapshot.minutes_ensemble is None:
            raise InvalidArgumentError("An exclusive minutes ensemble selection is required")
        return snapshot.minutes_ensemble, snapshot.external_send_policy


def _semantic_key(selection: MinutesSelectionInspection) -> tuple[str, int]:
    return selection.selection_scope_sha256, selection.cache_epoch


def _canonical_inspections(
    generators: tuple[EnsembleGeneratorInspection, ...],
) -> tuple[CanonicalGeneratorInspection, ...]:
    grouped: dict[tuple[str, int], list[EnsembleGeneratorInspection]] = {}
    for item in generators:
        grouped.setdefault(_semantic_key(item.selection), []).append(item)
    result = []
    for key, members in sorted(grouped.items()):
        shared_call_ordinal = len(result)
        for duplicate_ordinal, item in enumerate(members):
            result.append(
                CanonicalGeneratorInspection(
                    canonical_ordinal=len(result),
                    duplicate_ordinal=duplicate_ordinal,
                    shared_call_canonical_ordinal=shared_call_ordinal,
                    semantic_key=key,
                    selection=item.selection,
                    provenance=CanonicalGeneratorProvenance(
                        generator_id=item.generator_id,
                        label=item.label,
                        display_order=item.display_order,
                        duplicate_ordinal=duplicate_ordinal,
                    ),
                )
            )
    return tuple(result)


def _canonical_bindings(
    generators: tuple[ResolvedEnsembleGenerator, ...],
) -> tuple[CanonicalResolvedGenerator, ...]:
    grouped: dict[tuple[str, int], list[ResolvedEnsembleGenerator]] = {}
    for item in generators:
        grouped.setdefault(_semantic_key(item.binding.inspection), []).append(item)
    result = []
    for key, members in sorted(grouped.items()):
        shared_call_ordinal = len(result)
        shared_binding = members[0].binding
        for duplicate_ordinal, item in enumerate(members):
            result.append(
                CanonicalResolvedGenerator(
                    canonical_ordinal=len(result),
                    duplicate_ordinal=duplicate_ordinal,
                    shared_call_canonical_ordinal=shared_call_ordinal,
                    semantic_key=key,
                    binding=shared_binding,
                    provenance=CanonicalGeneratorProvenance(
                        generator_id=item.generator_id,
                        label=item.label,
                        display_order=item.display_order,
                        duplicate_ordinal=duplicate_ordinal,
                    ),
                )
            )
    return tuple(result)
