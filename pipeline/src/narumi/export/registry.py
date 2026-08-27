"""Exporter registry (``markdown`` / ``html`` / ``notion`` / ``gaia-library``)."""

from __future__ import annotations

from typing import Any

from narumi.errors import NotFoundError
from narumi.export.base import Exporter
from narumi.export.gaia import GaiaExporter
from narumi.export.html import HtmlExporter
from narumi.export.markdown import MarkdownExporter
from narumi.export.notion import NotionExporter

EXPORTERS: dict[str, Exporter] = {
    MarkdownExporter.name: MarkdownExporter(),
    HtmlExporter.name: HtmlExporter(),
    NotionExporter.name: NotionExporter(),
    GaiaExporter.name: GaiaExporter(),
}


def get_exporter(name: str) -> Exporter:
    try:
        return EXPORTERS[name]
    except KeyError:
        raise NotFoundError(
            f"unknown export destination: {name}", details={"available": list(EXPORTERS)}
        ) from None


def list_exporters() -> list[dict[str, Any]]:
    return [
        {"name": e.name, "description": e.description, "options_schema": e.options_schema}
        for e in EXPORTERS.values()
    ]
