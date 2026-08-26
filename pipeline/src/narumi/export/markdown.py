"""``markdown`` exporter: copy ``minutes/v<n>/minutes.md`` (+ slides) to a file path."""

from __future__ import annotations

from typing import Any

from narumi.bundle import Bundle, utc_now_iso
from narumi.export.base import ExportOutcome
from narumi.export.common import (
    PATH_OPTIONS_SCHEMA,
    copy_slides,
    minutes_markdown_path,
    resolve_destination,
)

__all__ = ["PATH_OPTIONS_SCHEMA", "MarkdownExporter"]


class MarkdownExporter:
    name = "markdown"
    description = "議事録 Markdown をファイルにコピーする（slides/ があれば隣に複製）"
    options_schema = PATH_OPTIONS_SCHEMA
    suffix = ".md"

    def export(
        self, bundle: Bundle, *, minutes_version: int, options: dict[str, Any]
    ) -> ExportOutcome:
        source = minutes_markdown_path(bundle, minutes_version)
        target = resolve_destination(bundle, minutes_version, options, self.suffix)
        text, slides = copy_slides(source.parent, target)
        target.path.write_text(text, encoding="utf-8")
        return ExportOutcome(
            destination=self.name,
            ref=str(target.path.resolve()),
            minutes_version=minutes_version,
            at=utc_now_iso(),
            details={"bytes": len(text.encode("utf-8")), "slides": str(slides) if slides else None},
        )
