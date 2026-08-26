"""``html`` exporter: minutes markdown → standalone HTML (python-markdown)."""

from __future__ import annotations

import html
from typing import Any

from narumi.bundle import Bundle, utc_now_iso
from narumi.errors import EngineUnavailableError
from narumi.export.base import ExportOutcome
from narumi.export.common import (
    PATH_OPTIONS_SCHEMA,
    copy_slides,
    minutes_markdown_path,
    resolve_destination,
)

MARKDOWN_EXTENSIONS = ["tables", "fenced_code"]
CSS = """
body { font-family: -apple-system, "Hiragino Sans", "Noto Sans JP", sans-serif; line-height: 1.7;
       max-width: 52rem; margin: 2rem auto; padding: 0 1.25rem; color: #1f2328; }
h1, h2 { border-bottom: 1px solid #d0d7de; padding-bottom: .3em; }
table { border-collapse: collapse; margin: 1em 0; }
th, td { border: 1px solid #d0d7de; padding: .35em .75em; text-align: left; }
code, pre { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: .95em; }
pre { background: #f6f8fa; padding: .75em; overflow-x: auto; }
img { max-width: 100%; }
""".strip()


def render_html(markdown_text: str, *, title: str) -> str:
    try:
        import markdown
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise EngineUnavailableError(
            "python-markdown is not installed (uv sync --extra html)",
            details={"exporter": "html", "error": str(exc)},
        ) from exc
    body = markdown.markdown(markdown_text, extensions=MARKDOWN_EXTENSIONS, output_format="html")
    return (
        '<!DOCTYPE html>\n<html lang="ja">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{html.escape(title)}</title>\n<style>\n{CSS}\n</style>\n</head>\n"
        f"<body>\n{body}\n</body>\n</html>\n"
    )


class HtmlExporter:
    name = "html"
    description = "議事録 Markdown を単体 HTML に変換してファイルへ書き出す"
    options_schema = PATH_OPTIONS_SCHEMA
    suffix = ".html"

    def export(
        self, bundle: Bundle, *, minutes_version: int, options: dict[str, Any]
    ) -> ExportOutcome:
        source = minutes_markdown_path(bundle, minutes_version)
        target = resolve_destination(bundle, minutes_version, options, self.suffix)
        text, slides = copy_slides(source.parent, target)
        document = render_html(text, title=bundle.manifest.meeting_name)
        target.path.write_text(document, encoding="utf-8")
        return ExportOutcome(
            destination=self.name,
            ref=str(target.path.resolve()),
            minutes_version=minutes_version,
            at=utc_now_iso(),
            details={
                "bytes": len(document.encode("utf-8")),
                "slides": str(slides) if slides else None,
            },
        )
