"""Rebuild helpers: ``narumi.db`` is derived data and can always be regenerated from bundles."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class RebuildStats:
    """Outcome of a catalog rebuild."""

    meetings: int = 0
    segments: int = 0
    errors: list[str] = field(default_factory=list)


def rebuild_catalog(db_path: Path, meetings_root: Path, *, actor: str = "catalog") -> RebuildStats:
    """Open (or create) ``db_path`` and rebuild it from ``meetings_root``; closes the DB after."""
    from narumi.catalog.db import Catalog

    with Catalog(db_path) as catalog:
        return catalog.rebuild(meetings_root, actor=actor)
