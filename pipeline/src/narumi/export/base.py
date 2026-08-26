"""Exporter plugin protocol."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from narumi.bundle import Bundle


@dataclass
class ExportOutcome:
    destination: str
    ref: str
    minutes_version: int
    at: str
    details: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class Exporter(Protocol):
    name: str
    description: str
    options_schema: dict[str, Any] | None

    def export(
        self, bundle: Bundle, *, minutes_version: int, options: dict[str, Any]
    ) -> ExportOutcome:
        """Export one minutes version. Recording the outcome in the manifest is the caller's job."""
        ...
