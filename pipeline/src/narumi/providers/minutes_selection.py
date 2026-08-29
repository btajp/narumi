"""Immutable, non-secret projections of a verified minutes model selection."""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from narumi.bundle.hashing import canonical_json, sha256_params
from narumi.llm.base import CostClass, LLMProvider

JSONScalar = str | int | bool | None
SELECTION_SCOPE_SCHEMA_VERSION = "ensemble-minutes-selection-scope-v1"


@dataclass(frozen=True)
class AuthorizationPin:
    """The current authorization that must be rechecked before use or publication."""

    provider: str
    connection_id: str
    connection_revision: int
    external_send_policy: str
    auth_method: str
    data_destination: str
    cost_class: CostClass

    def to_dict(self) -> dict[str, JSONScalar]:
        return {
            "provider": self.provider,
            "connection_id": self.connection_id,
            "connection_revision": self.connection_revision,
            "external_send_policy": self.external_send_policy,
            "auth_method": self.auth_method,
            "data_destination": self.data_destination,
            "cost_class": self.cost_class,
        }


@dataclass(frozen=True)
class ConnectionScope:
    """Connection ownership for successful-result reuse, separate from content identity."""

    provider: str
    connection_id: str

    def to_dict(self) -> dict[str, str]:
        return {"provider": self.provider, "connection_id": self.connection_id}


@dataclass(frozen=True)
class SelectionObservations:
    """Non-secret observations used during validation, never a replacement for revalidation."""

    runtime_catalog_revision: str
    availability_expires_on: str | None

    def to_dict(self) -> dict[str, str | None]:
        return {
            "runtime_catalog_revision": self.runtime_catalog_revision,
            "availability_expires_on": self.availability_expires_on,
        }


@dataclass(frozen=True)
class MinutesContentConditions:
    """Model-side conditions used by ensemble request and unknown-barrier fingerprints."""

    schema_version: str
    provider: str
    endpoint: str
    model_id: str
    resolved_revision: str | None
    effective_parameters: Mapping[str, JSONScalar]
    context_window: int | None
    max_output_tokens: int | None
    model_capabilities_sha256: str
    runtime_version: str
    runtime_sha256: str
    adapter_version: str
    capability_table_version: str | None

    @classmethod
    def create(
        cls,
        *,
        provider: str,
        endpoint: str,
        model_id: str,
        resolved_revision: str | None,
        effective_parameters: Mapping[str, JSONScalar],
        context_window: int | None,
        max_output_tokens: int | None,
        model_capabilities_sha256: str,
        runtime_version: str,
        runtime_sha256: str,
        adapter_version: str,
        capability_table_version: str | None,
    ) -> MinutesContentConditions:
        # Current parameters are scalar. Canonicalization also rejects accidental
        # non-JSON values before this projection can become durable identity.
        values = copy.deepcopy(dict(effective_parameters))
        if any(not isinstance(key, str) for key in values):
            raise TypeError("Minutes parameters must use string keys")
        if any(
            value is not None and type(value) not in (str, int, bool) for value in values.values()
        ):
            raise TypeError("Minutes parameters must use scalar JSON values")
        canonical_json(values)
        return cls(
            schema_version="ensemble-minutes-model-v1",
            provider=provider,
            endpoint=endpoint,
            model_id=model_id,
            resolved_revision=resolved_revision,
            effective_parameters=MappingProxyType(values),
            context_window=context_window,
            max_output_tokens=max_output_tokens,
            model_capabilities_sha256=model_capabilities_sha256,
            runtime_version=runtime_version,
            runtime_sha256=runtime_sha256,
            adapter_version=adapter_version,
            capability_table_version=capability_table_version,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "provider": self.provider,
            "endpoint": self.endpoint,
            "model_id": self.model_id,
            "resolved_revision": self.resolved_revision,
            "effective_parameters": dict(self.effective_parameters),
            "context_window": self.context_window,
            "max_output_tokens": self.max_output_tokens,
            "model_capabilities_sha256": self.model_capabilities_sha256,
            "runtime_version": self.runtime_version,
            "runtime_sha256": self.runtime_sha256,
            "adapter_version": self.adapter_version,
            "capability_table_version": self.capability_table_version,
        }


@dataclass(frozen=True)
class MinutesSelectionInspection:
    """Verified selection projections without credentials or mutable provider metadata."""

    authorization: AuthorizationPin
    connection_scope: ConnectionScope
    observations: SelectionObservations
    content_conditions: MinutesContentConditions
    content_conditions_sha256: str
    selection_scope_sha256: str
    cache_epoch: int
    _legacy_generation_params_json: str = field(repr=False)

    @classmethod
    def create(
        cls,
        *,
        authorization: AuthorizationPin,
        connection_scope: ConnectionScope,
        observations: SelectionObservations,
        content_conditions: MinutesContentConditions,
        content_conditions_sha256: str,
        cache_epoch: int,
        legacy_generation_params: Mapping[str, Any],
    ) -> MinutesSelectionInspection:
        if type(cache_epoch) is not int or cache_epoch < 0:
            raise ValueError("The minutes cache epoch must be a non-negative integer")
        selection_scope_sha256 = sha256_params(
            {
                "schema_version": SELECTION_SCOPE_SCHEMA_VERSION,
                "connection_scope": connection_scope.to_dict(),
                "content_conditions_sha256": content_conditions_sha256,
            }
        )
        return cls(
            authorization=authorization,
            connection_scope=connection_scope,
            observations=observations,
            content_conditions=content_conditions,
            content_conditions_sha256=content_conditions_sha256,
            selection_scope_sha256=selection_scope_sha256,
            cache_epoch=cache_epoch,
            _legacy_generation_params_json=canonical_json(dict(legacy_generation_params)),
        )

    @property
    def legacy_generation_params(self) -> dict[str, Any]:
        """Return an isolated copy so callers cannot mutate the verified snapshot."""
        return json.loads(self._legacy_generation_params_json)


@dataclass(frozen=True)
class BoundMinutesSelection:
    """A verified inspection paired with the existing guarded provider adapter."""

    inspection: MinutesSelectionInspection
    provider: LLMProvider
