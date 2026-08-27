from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from narumi.bundle import Bundle, MinutesVersionRecord
from narumi.errors import InvalidArgumentError, NotFoundError
from narumi.export import (
    EXPORTERS,
    Exporter,
    ExportOutcome,
    HtmlExporter,
    MarkdownExporter,
    get_exporter,
    list_exporters,
    render_html,
)

MINUTES = """# 定例 <会議>

| 項目 | 値 |
| --- | --- |
| 会議 ID | x |

## 決定事項

- 決まった

```text
code
```

![slide](slides/001.png)
"""


def bundle_with_minutes(tmp_path: Path, *, slides: bool = False) -> Bundle:
    bundle = Bundle.create(tmp_path / "meetings", meeting_name="定例 <会議>")
    v1 = bundle.minutes_dir(1)
    (v1 / "minutes.md").write_text(MINUTES, encoding="utf-8")
    if slides:
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


def test_registry():
    assert list(EXPORTERS) == ["markdown", "html", "notion", "gaia-library"]
    assert isinstance(get_exporter("markdown"), MarkdownExporter)
    assert isinstance(get_exporter("html"), HtmlExporter)
    assert all(isinstance(e, Exporter) for e in EXPORTERS.values())
    with pytest.raises(NotFoundError):
        get_exporter("nope")
    listed = list_exporters()
    assert [e["name"] for e in listed] == ["markdown", "html", "notion", "gaia-library"]
    for entry in listed:
        assert entry["description"]
        schema = entry["options_schema"]
        if entry["name"] in ("markdown", "html"):
            assert set(schema["properties"]) == {"output_path", "overwrite"}
        assert schema["additionalProperties"] is False
        Draft202012Validator.check_schema(schema)


def test_markdown_export_to_explicit_path(tmp_path: Path):
    bundle = bundle_with_minutes(tmp_path)
    dest = tmp_path / "out" / "minutes.md"
    outcome = get_exporter("markdown").export(
        bundle, minutes_version=1, options={"output_path": str(dest)}
    )
    assert isinstance(outcome, ExportOutcome)
    assert outcome.destination == "markdown" and outcome.minutes_version == 1
    assert outcome.ref == str(dest.resolve())
    assert dest.read_text(encoding="utf-8") == MINUTES
    assert outcome.details["slides"] is None and outcome.at.endswith("Z")


def test_export_never_clobbers_existing_files_without_overwrite(tmp_path: Path):
    bundle = bundle_with_minutes(tmp_path, slides=True)
    precious = tmp_path / "precious.md"
    precious.write_text("keep me\n", encoding="utf-8")
    exporter = get_exporter("markdown")
    with pytest.raises(InvalidArgumentError) as excinfo:
        exporter.export(bundle, minutes_version=1, options={"output_path": str(precious)})
    assert "overwrite" in str(excinfo.value)
    assert precious.read_text(encoding="utf-8") == "keep me\n"
    # a pre-existing <stem>-slides directory is never rmtree'd either
    fresh = tmp_path / "fresh.md"
    foreign = tmp_path / "fresh-slides"
    foreign.mkdir()
    (foreign / "mine.txt").write_text("x", encoding="utf-8")
    with pytest.raises(InvalidArgumentError):
        exporter.export(bundle, minutes_version=1, options={"output_path": str(fresh)})
    assert (foreign / "mine.txt").exists() and not fresh.exists()
    # explicit consent replaces both
    outcome = exporter.export(
        bundle, minutes_version=1, options={"output_path": str(precious), "overwrite": True}
    )
    assert precious.read_text(encoding="utf-8") != "keep me\n"
    assert Path(outcome.details["slides"]) == tmp_path / "precious-slides"
    # relative paths, ``~`` and unknown keys are rejected before anything is written
    for options in (
        {"output_path": "relative/out.md"},
        {"output_path": "~/out.md"},
        {"output_path": str(tmp_path / "x.md"), "path": "legacy"},
        {"output_path": str(tmp_path / "x.md"), "overwrite": "yes"},
        {"output_path": str(tmp_path)},
    ):
        with pytest.raises(InvalidArgumentError):
            exporter.export(bundle, minutes_version=1, options=options)
    assert not (tmp_path / "x.md").exists()


def test_markdown_export_default_path_and_slides(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NARUMI_HOME", str(tmp_path / "home"))
    bundle = bundle_with_minutes(tmp_path, slides=True)
    outcome = get_exporter("markdown").export(bundle, minutes_version=1, options={})
    expected = tmp_path / "home" / "exports" / f"{bundle.meeting_id}-v1.md"
    assert outcome.ref == str(expected.resolve())
    text = expected.read_text(encoding="utf-8")
    assert f"![slide]({bundle.meeting_id}-v1-slides/001.png)" in text
    assert (expected.parent / f"{bundle.meeting_id}-v1-slides" / "001.png").read_bytes() == b"png"
    assert outcome.details["slides"] == str(expected.parent / f"{bundle.meeting_id}-v1-slides")
    # the default location is narumi-managed: re-exporting replaces it without ``overwrite``
    again = get_exporter("markdown").export(bundle, minutes_version=1, options={})
    assert again.ref == outcome.ref


def test_html_export(tmp_path: Path):
    bundle = bundle_with_minutes(tmp_path, slides=True)
    dest = tmp_path / "out.html"
    outcome = get_exporter("html").export(
        bundle, minutes_version=1, options={"output_path": str(dest)}
    )
    html = dest.read_text(encoding="utf-8")
    assert html.startswith("<!DOCTYPE html>")
    assert '<html lang="ja">' in html
    assert "<title>定例 &lt;会議&gt;</title>" in html
    assert "<table>" in html and "<td>x</td>" in html
    assert '<code class="language-text">code' in html
    assert 'src="out-slides/001.png"' in html
    assert outcome.ref == str(dest.resolve()) and outcome.destination == "html"


def test_render_html_standalone():
    html = render_html("# T\n\n| a | b |\n| - | - |\n| 1 | 2 |\n", title="x & y")
    assert "<title>x &amp; y</title>" in html and "<td>2</td>" in html and "<style>" in html


def test_export_errors(tmp_path: Path):
    bundle = bundle_with_minutes(tmp_path)
    with pytest.raises(NotFoundError):
        get_exporter("markdown").export(bundle, minutes_version=2, options={})
    with pytest.raises(InvalidArgumentError):
        get_exporter("markdown").export(bundle, minutes_version=1, options={"output_path": ""})
    (bundle.path / "minutes/v1/minutes.md").unlink()
    with pytest.raises(NotFoundError):
        get_exporter("html").export(
            bundle, minutes_version=1, options={"output_path": str(tmp_path / "o")}
        )
