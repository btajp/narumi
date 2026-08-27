"""``notion`` exporter: create a Notion page from a minutes version via the REST API.

Talks to the Notion REST API (version header ``2022-06-28``) with stdlib ``urllib`` — no SDK.
The minutes markdown is converted best-effort to Notion blocks: headings, paragraphs, bulleted
lists, tables and code fences. Rich text is chunked to Notion's 2000-character-per-text limit and
children are appended in batches of at most 100 blocks (page create carries the first batch,
``PATCH /v1/blocks/<id>/children`` the rest).

Limitation — slide images stay local: the Notion file-upload flow is multi-step (create upload →
send bytes → attach) and ``external`` image URLs cannot point at local files, so slide references
are dropped from the page and a callout block notes where the images live on disk.

The integration token is read from the environment (``options.token_env``, default
``NOTION_TOKEN``) at export time and never appears in logs or error messages.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from narumi.bundle import Bundle, utc_now_iso
from narumi.errors import EngineUnavailableError, InvalidArgumentError, NarumiError
from narumi.export.base import ExportOutcome
from narumi.export.common import SLIDES_DIR, minutes_markdown_path

NOTION_VERSION = "2022-06-28"
DEFAULT_TOKEN_ENV = "NOTION_TOKEN"
DEFAULT_API_BASE = "https://api.notion.com"
ENV_API_BASE = "NARUMI_NOTION_API_URL"
"""Override the API base URL (tests point it at an in-process fake server)."""

MAX_TEXT_CHARS = 2000
MAX_CHILDREN_PER_REQUEST = 100
_ERROR_BODY_TAIL = 500

OPTIONS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "parent_page_id": {
            "type": "string",
            "minLength": 1,
            "description": "Notion page ID to create the minutes page under.",
        },
        "database_id": {
            "type": "string",
            "minLength": 1,
            "description": "Notion database ID to create the minutes page in.",
        },
        "token_env": {
            "type": "string",
            "minLength": 1,
            "default": DEFAULT_TOKEN_ENV,
            "description": (
                "Environment variable holding the Notion integration token "
                f"(default {DEFAULT_TOKEN_ENV}). The token itself never goes through MCP."
            ),
        },
    },
    "additionalProperties": False,
    "oneOf": [{"required": ["parent_page_id"]}, {"required": ["database_id"]}],
}

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_IMAGE_RE = re.compile(r"^!\[[^\]]*\]\(([^)]+)\)\s*$")
_TABLE_SEPARATOR_RE = re.compile(r"^\|(:?-+:?\|)+$")

_CODE_LANGUAGES = frozenset(
    {
        "bash", "c", "c++", "css", "html", "java", "javascript", "json", "markdown",
        "plain text", "python", "ruby", "rust", "shell", "sql", "swift", "typescript", "yaml",
    }
)  # fmt: skip
_FALLBACK_CODE_LANGUAGE = "plain text"


class NotionExporter:
    name = "notion"
    description = (
        "議事録を Notion ページとして作成する（REST API 2022-06-28。要 parent_page_id か "
        "database_id、トークンは token_env の環境変数から）。スライド画像はローカル保存のままで"
        "ページには含まれず、保存場所を callout で明記する"
    )
    options_schema = OPTIONS_SCHEMA

    def __init__(self, api_base: str | None = None, *, timeout: float = 30.0) -> None:
        self._api_base = api_base
        self.timeout = timeout

    def export(
        self, bundle: Bundle, *, minutes_version: int, options: dict[str, Any]
    ) -> ExportOutcome:
        parent, token_env = _validate_options(options)
        token = os.environ.get(token_env, "").strip()
        if not token:
            raise EngineUnavailableError(
                f"Notion token not found: set ${token_env} (or pass options.token_env)",
                details={"token_env": token_env},
            )
        source = minutes_markdown_path(bundle, minutes_version)
        blocks, slide_count, title = markdown_to_blocks(source.read_text(encoding="utf-8"))
        page_title = title or f"{bundle.manifest.meeting_name} 議事録 v{minutes_version}"
        if slide_count:
            blocks.append(_slides_callout(source.parent / SLIDES_DIR, slide_count))

        page = self._request(
            "POST",
            "/v1/pages",
            {
                "parent": parent,
                "properties": {"title": {"title": _rich_text(page_title)}},
                "children": blocks[:MAX_CHILDREN_PER_REQUEST],
            },
            token,
        )
        page_id = str(page.get("id") or "")
        batches = 1
        for start in range(MAX_CHILDREN_PER_REQUEST, len(blocks), MAX_CHILDREN_PER_REQUEST):
            self._request(
                "PATCH",
                f"/v1/blocks/{page_id}/children",
                {"children": blocks[start : start + MAX_CHILDREN_PER_REQUEST]},
                token,
            )
            batches += 1
        return ExportOutcome(
            destination=self.name,
            ref=str(page.get("url") or f"notion://page/{page_id}"),
            minutes_version=minutes_version,
            at=utc_now_iso(),
            details={
                "page_id": page_id,
                "blocks": len(blocks),
                "batches": batches,
                "slides_left_local": slide_count,
            },
        )

    # ------------------------------------------------------------------ transport
    def _request(
        self, method: str, path: str, payload: dict[str, Any], token: str
    ) -> dict[str, Any]:
        base = self._api_base or os.environ.get(ENV_API_BASE, "").strip() or DEFAULT_API_BASE
        request = urllib.request.Request(
            base.rstrip("/") + path,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            method=method,
            headers={
                "Authorization": f"Bearer {token}",
                "Notion-Version": NOTION_VERSION,
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:  # noqa: S310
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as err:
            tail = err.read().decode("utf-8", errors="replace")[-_ERROR_BODY_TAIL:]
            raise NarumiError(
                f"Notion API {err.code} on {method} {path}: {tail}",
                details={"status": err.code, "path": path},
            ) from None
        except (urllib.error.URLError, OSError) as err:
            reason = getattr(err, "reason", None) or err
            raise EngineUnavailableError(
                f"Notion API unreachable: {reason}", details={"path": path}
            ) from None
        try:
            parsed = json.loads(body)
        except ValueError as err:
            raise NarumiError(
                f"Notion API returned invalid JSON on {method} {path}: {err}"
            ) from None
        if not isinstance(parsed, dict):
            raise NarumiError(f"Notion API returned a non-object on {method} {path}")
        return parsed


# ---------------------------------------------------------------------------- options
def _validate_options(options: dict[str, Any]) -> tuple[dict[str, str], str]:
    """Defensive re-check of ``OPTIONS_SCHEMA`` (the server validates before calling, MCP-side)."""
    unknown = sorted(set(options) - set(OPTIONS_SCHEMA["properties"]))
    if unknown:
        raise InvalidArgumentError(
            f"unknown export options: {', '.join(unknown)}", details={"unknown": unknown}
        )
    given = [key for key in ("parent_page_id", "database_id") if options.get(key) is not None]
    if len(given) != 1:
        raise InvalidArgumentError(
            "exactly one of parent_page_id / database_id is required", details={"given": given}
        )
    key = given[0]
    value = options[key]
    if not isinstance(value, str) or not value.strip():
        raise InvalidArgumentError(f"options.{key} must be a non-empty string")
    token_env = options.get("token_env", DEFAULT_TOKEN_ENV)
    if not isinstance(token_env, str) or not token_env.strip():
        raise InvalidArgumentError("options.token_env must be a non-empty string")
    parent_key = "page_id" if key == "parent_page_id" else "database_id"
    return {parent_key: value.strip()}, token_env.strip()


# ---------------------------------------------------------------------------- markdown → blocks
def markdown_to_blocks(markdown: str) -> tuple[list[dict[str, Any]], int, str | None]:
    """Best-effort markdown → Notion blocks.

    Returns ``(blocks, slide_count, title)``: a leading H1 becomes the page title (not a block),
    image references are dropped and counted (slides stay local), everything unrecognized becomes
    a paragraph.
    """
    blocks: list[dict[str, Any]] = []
    slides = 0
    title: str | None = None
    paragraph: list[str] = []
    lines = markdown.splitlines()

    def flush_paragraph() -> None:
        if paragraph:
            text = "\n".join(paragraph).strip()
            if text:
                blocks.append(_text_block("paragraph", text))
            paragraph.clear()

    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        if not stripped:
            flush_paragraph()
            i += 1
            continue
        if stripped.startswith("```"):
            flush_paragraph()
            language = stripped[3:].strip().lower()
            code_lines: list[str] = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code_lines.append(lines[i])
                i += 1
            i += 1  # closing fence (or EOF)
            blocks.append(
                {
                    "object": "block",
                    "type": "code",
                    "code": {
                        "language": language
                        if language in _CODE_LANGUAGES
                        else _FALLBACK_CODE_LANGUAGE,
                        "rich_text": _rich_text("\n".join(code_lines)),
                    },
                }
            )
            continue
        heading = _HEADING_RE.match(stripped)
        if heading:
            flush_paragraph()
            level = len(heading.group(1))
            content = heading.group(2).strip()
            if level == 1 and title is None and not blocks:
                title = content
            else:
                blocks.append(_text_block(f"heading_{min(level, 3)}", content))
            i += 1
            continue
        if _IMAGE_RE.match(stripped):
            flush_paragraph()
            slides += 1
            i += 1
            continue
        if stripped.startswith(("- ", "* ")):
            flush_paragraph()
            blocks.append(_text_block("bulleted_list_item", stripped[2:].strip()))
            i += 1
            continue
        if stripped.startswith("|") and stripped.endswith("|"):
            flush_paragraph()
            table_lines: list[str] = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                table_lines.append(lines[i].strip())
                i += 1
            table = _table_block(table_lines)
            if table is not None:
                blocks.append(table)
            continue
        paragraph.append(stripped)
        i += 1
    flush_paragraph()
    return blocks, slides, title


def _rich_text(text: str) -> list[dict[str, Any]]:
    """Chunk ``text`` into Notion rich_text items of at most :data:`MAX_TEXT_CHARS` chars."""
    chunks = [text[i : i + MAX_TEXT_CHARS] for i in range(0, len(text), MAX_TEXT_CHARS)] or [""]
    return [{"type": "text", "text": {"content": chunk}} for chunk in chunks]


def _text_block(kind: str, text: str) -> dict[str, Any]:
    return {"object": "block", "type": kind, kind: {"rich_text": _rich_text(text)}}


def _table_block(table_lines: list[str]) -> dict[str, Any] | None:
    rows: list[list[str]] = []
    for line in table_lines:
        if _TABLE_SEPARATOR_RE.match(line.replace(" ", "")):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        rows.append(cells)
    if not rows:
        return None
    width = max(len(row) for row in rows)
    children = [
        {
            "object": "block",
            "type": "table_row",
            "table_row": {"cells": [_rich_text(cell) for cell in row + [""] * (width - len(row))]},
        }
        for row in rows
    ]
    return {
        "object": "block",
        "type": "table",
        "table": {"table_width": width, "has_column_header": True, "children": children},
    }


def _slides_callout(slides_dir: Path, count: int) -> dict[str, Any]:
    text = (
        f"スライド画像 {count} 枚はローカルの {slides_dir} に保存されています。"
        "Notion API ではローカルファイルをこの経路で添付できないため、ページには含まれていません。"
    )
    return {
        "object": "block",
        "type": "callout",
        "callout": {"icon": {"type": "emoji", "emoji": "🖼️"}, "rich_text": _rich_text(text)},
    }
