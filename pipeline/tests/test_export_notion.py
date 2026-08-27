"""Notion exporter against an in-process fake Notion REST API (http.server thread)."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from narumi.bundle import Bundle, MinutesVersionRecord
from narumi.errors import EngineUnavailableError, InvalidArgumentError, NarumiError
from narumi.export.notion import (
    MAX_CHILDREN_PER_REQUEST,
    MAX_TEXT_CHARS,
    NotionExporter,
    markdown_to_blocks,
)
from narumi.export.registry import EXPORTERS, get_exporter

TOKEN = "secret-token-do-not-log"
LONG_PARAGRAPH = "あ" * (MAX_TEXT_CHARS * 2 + 10)
BULLETS = "\n".join(f"- 決定事項 {i}" for i in range(120))
MINUTES = f"""# 定例ミーティング 議事録

| 項目 | 値 |
| --- | --- |
| 会議 ID | x-1 |

## 決定事項

{BULLETS}

{LONG_PARAGRAPH}

```text
raw log line
```

![slide](slides/001.png)
"""


class FakeNotionServer:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.fail_next_status: int | None = None
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        assert self._httpd is not None
        return f"http://127.0.0.1:{self._httpd.server_address[1]}"

    def start(self) -> None:
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def do_POST(self) -> None:
                self._handle("POST")

            def do_PATCH(self) -> None:
                self._handle("PATCH")

            def _handle(self, method: str) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length)) if length else {}
                server.requests.append(
                    {
                        "method": method,
                        "path": self.path,
                        "headers": dict(self.headers.items()),
                        "body": body,
                    }
                )
                if server.fail_next_status is not None:
                    status = server.fail_next_status
                    server.fail_next_status = None
                    self._reply(
                        status,
                        {"object": "error", "status": status, "message": "validation failed"},
                    )
                    return
                if method == "POST" and self.path == "/v1/pages":
                    self._reply(
                        200,
                        {"object": "page", "id": "page-1", "url": "https://www.notion.so/page-1"},
                    )
                    return
                if (
                    method == "PATCH"
                    and self.path.startswith("/v1/blocks/")
                    and self.path.endswith("/children")
                ):
                    self._reply(200, {"object": "list", "results": []})
                    return
                self._reply(404, {"object": "error", "status": 404, "message": "unknown path"})

            def _reply(self, status: int, payload: dict[str, Any]) -> None:
                raw = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)


@pytest.fixture()
def notion_server():
    server = FakeNotionServer()
    server.start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture()
def exporter(notion_server: FakeNotionServer) -> NotionExporter:
    return NotionExporter(api_base=notion_server.base_url)


def bundle_with_minutes(tmp_path: Path, markdown: str = MINUTES) -> Bundle:
    bundle = Bundle.create(tmp_path / "meetings", meeting_name="定例ミーティング")
    v1 = bundle.minutes_dir(1)
    (v1 / "minutes.md").write_text(markdown, encoding="utf-8")
    (v1 / "slides").mkdir()
    (v1 / "slides" / "001.png").write_bytes(b"png")
    bundle.manifest.minutes_versions.append(
        MinutesVersionRecord(
            version=1,
            path="minutes/v1/minutes.md",
            generated_at="2026-08-27T03:10:00Z",
            provider="none",
        )
    )
    bundle.save()
    return bundle


def all_children(server: FakeNotionServer) -> list[dict[str, Any]]:
    children: list[dict[str, Any]] = []
    for request in server.requests:
        children.extend(request["body"].get("children") or [])
    return children


# ---------------------------------------------------------------------------- happy path
def test_export_creates_page_with_batched_blocks(
    tmp_path: Path, notion_server: FakeNotionServer, exporter: NotionExporter, monkeypatch
):
    monkeypatch.setenv("NOTION_TOKEN", TOKEN)
    bundle = bundle_with_minutes(tmp_path)
    outcome = exporter.export(bundle, minutes_version=1, options={"parent_page_id": "parent-1"})

    assert outcome.destination == "notion" and outcome.minutes_version == 1
    assert outcome.ref == "https://www.notion.so/page-1"
    assert outcome.details["page_id"] == "page-1"

    create = notion_server.requests[0]
    assert (create["method"], create["path"]) == ("POST", "/v1/pages")
    assert create["headers"]["Authorization"] == f"Bearer {TOKEN}"
    assert create["headers"]["Notion-Version"] == "2022-06-28"
    assert create["body"]["parent"] == {"page_id": "parent-1"}
    # the markdown H1 became the page title, not a block
    title = create["body"]["properties"]["title"]["title"]
    assert title[0]["text"]["content"] == "定例ミーティング 議事録"

    # children batching: every request carries at most 100 blocks, nothing is lost
    for request in notion_server.requests:
        assert len(request["body"].get("children") or []) <= MAX_CHILDREN_PER_REQUEST
    patches = notion_server.requests[1:]
    assert all((r["method"], r["path"]) == ("PATCH", "/v1/blocks/page-1/children") for r in patches)
    children = all_children(notion_server)
    assert len(children) == outcome.details["blocks"] > MAX_CHILDREN_PER_REQUEST
    assert outcome.details["batches"] == 1 + len(patches)

    kinds = [block["type"] for block in children]
    assert "table" in kinds and "code" in kinds and "heading_2" in kinds
    assert kinds.count("bulleted_list_item") == 120
    assert "image" not in kinds
    # the long paragraph was chunked to Notion's 2000-char rich_text limit
    paragraph = next(b for b in children if b["type"] == "paragraph")
    chunks = [item["text"]["content"] for item in paragraph["paragraph"]["rich_text"]]
    assert len(chunks) == 3 and all(len(chunk) <= MAX_TEXT_CHARS for chunk in chunks)
    assert "".join(chunks) == LONG_PARAGRAPH
    table = next(b for b in children if b["type"] == "table")
    assert table["table"]["table_width"] == 2
    assert [
        c[0]["text"]["content"] for c in table["table"]["children"][0]["table_row"]["cells"]
    ] == [
        "項目",
        "値",
    ]
    # slides stay local: the last block is a callout saying where they are
    assert outcome.details["slides_left_local"] == 1
    callout = children[-1]
    assert callout["type"] == "callout"
    note = callout["callout"]["rich_text"][0]["text"]["content"]
    assert "スライド画像 1 枚" in note and "minutes/v1/slides" in note


def test_database_parent_and_custom_token_env(
    tmp_path: Path, notion_server: FakeNotionServer, exporter: NotionExporter, monkeypatch
):
    monkeypatch.delenv("NOTION_TOKEN", raising=False)
    monkeypatch.setenv("MY_NOTION_TOKEN", "other-token")
    bundle = bundle_with_minutes(tmp_path, markdown="# T\n\nhello\n")
    exporter.export(
        bundle,
        minutes_version=1,
        options={"database_id": "db-1", "token_env": "MY_NOTION_TOKEN"},
    )
    create = notion_server.requests[0]
    assert create["body"]["parent"] == {"database_id": "db-1"}
    assert create["headers"]["Authorization"] == "Bearer other-token"


# ---------------------------------------------------------------------------- validation
def test_option_validation(tmp_path: Path, exporter: NotionExporter, monkeypatch):
    monkeypatch.setenv("NOTION_TOKEN", TOKEN)
    bundle = bundle_with_minutes(tmp_path, markdown="# T\n")
    for options in (
        {},  # neither parent given
        {"parent_page_id": "p", "database_id": "d"},  # both given
        {"parent_page_id": ""},
        {"parent_page_id": "p", "token_env": ""},
        {"parent_page_id": "p", "page_size": 10},  # unknown option
    ):
        with pytest.raises(InvalidArgumentError):
            exporter.export(bundle, minutes_version=1, options=options)


def test_missing_token_is_engine_unavailable(
    tmp_path: Path, notion_server: FakeNotionServer, exporter: NotionExporter, monkeypatch
):
    monkeypatch.delenv("NOTION_TOKEN", raising=False)
    bundle = bundle_with_minutes(tmp_path, markdown="# T\n")
    with pytest.raises(EngineUnavailableError) as excinfo:
        exporter.export(bundle, minutes_version=1, options={"parent_page_id": "p"})
    assert "NOTION_TOKEN" in str(excinfo.value)
    assert notion_server.requests == []  # nothing was sent


def test_http_error_carries_body_tail_but_never_the_token(
    tmp_path: Path, notion_server: FakeNotionServer, exporter: NotionExporter, monkeypatch
):
    monkeypatch.setenv("NOTION_TOKEN", TOKEN)
    bundle = bundle_with_minutes(tmp_path, markdown="# T\n\nhello\n")
    notion_server.fail_next_status = 400
    with pytest.raises(NarumiError) as excinfo:
        exporter.export(bundle, minutes_version=1, options={"parent_page_id": "p"})
    message = str(excinfo.value)
    assert "400" in message and "validation failed" in message
    assert TOKEN not in message and TOKEN not in json.dumps(excinfo.value.details)


# ---------------------------------------------------------------------------- registry / schema
def test_notion_exporter_is_registered():
    exporter = get_exporter("notion")
    assert isinstance(exporter, NotionExporter)
    assert "notion" in EXPORTERS
    Draft202012Validator.check_schema(exporter.options_schema)
    validator = Draft202012Validator(exporter.options_schema)
    assert validator.is_valid({"parent_page_id": "p"})
    assert validator.is_valid({"database_id": "d", "token_env": "X"})
    assert not validator.is_valid({})
    assert not validator.is_valid({"parent_page_id": "p", "database_id": "d"})
    assert not validator.is_valid({"parent_page_id": "p", "extra": 1})


# ---------------------------------------------------------------------------- conversion details
def test_markdown_to_blocks_title_and_fallbacks():
    blocks, slides, title = markdown_to_blocks(
        "# タイトル\n\n#### 深い見出し\n\n```nope\nx\n```\n\n"
        "![a](slides/1.png)\n![b](slides/2.png)\n"
    )
    assert title == "タイトル"
    assert slides == 2
    assert [b["type"] for b in blocks] == ["heading_3", "code"]
    assert blocks[1]["code"]["language"] == "plain text"
    # a second H1 further down stays a heading (only a leading H1 becomes the title)
    blocks2, _, title2 = markdown_to_blocks("intro\n\n# あとから\n")
    assert title2 is None
    assert [b["type"] for b in blocks2] == ["paragraph", "heading_1"]
