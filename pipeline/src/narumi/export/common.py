"""Helpers shared by file-based exporters."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from narumi.bundle import Bundle
from narumi.config import data_root
from narumi.errors import InvalidArgumentError, NotFoundError

SLIDES_DIR = "slides"
OUTPUT_PATH_OPTION = "output_path"
OVERWRITE_OPTION = "overwrite"

PATH_OPTIONS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        OUTPUT_PATH_OPTION: {
            "type": "string",
            "minLength": 1,
            "description": (
                "Absolute output file path. Default: <NARUMI_HOME>/exports/<meeting_id>-v<n>.<ext>"
                " (narumi-managed; replaced on re-export)."
            ),
        },
        OVERWRITE_OPTION: {
            "type": "boolean",
            "default": False,
            "description": (
                "Replace an existing file (and <stem>-slides directory) at output_path. Default "
                "false: an existing target is rejected with invalid_argument."
            ),
        },
    },
    "additionalProperties": False,
}
"""``options_schema`` of the file exporters (markdown / html). Enforced by the server before the
exporter runs and defensively by :func:`resolve_destination`."""


@dataclass(frozen=True)
class FileTarget:
    """Where a file exporter writes and whether it may replace what is already there."""

    path: Path
    overwrite: bool

    @property
    def slides_dir(self) -> Path:
        return self.path.parent / f"{self.path.stem}-{SLIDES_DIR}"


def minutes_markdown_path(bundle: Bundle, minutes_version: int) -> Path:
    """Path of ``minutes/v<n>/minutes.md`` for a recorded version (``NotFoundError`` otherwise)."""
    record = next(
        (r for r in bundle.manifest.minutes_versions if r.version == minutes_version), None
    )
    if record is None:
        raise NotFoundError(
            f"minutes version {minutes_version} not found",
            details={
                "meeting_id": bundle.meeting_id,
                "versions": [r.version for r in bundle.manifest.minutes_versions],
            },
        )
    path = bundle.abspath(record.path)
    if not path.exists():
        raise NotFoundError(f"minutes file missing: {record.path}", details={"path": str(path)})
    return path


def resolve_destination(
    bundle: Bundle, minutes_version: int, options: dict[str, Any], suffix: str
) -> FileTarget:
    """``options["output_path"]`` or ``<data_root>/exports/<meeting_id>-v<n><suffix>``.

    An explicit ``output_path`` must be absolute (no ``~`` expansion) and must not exist unless
    ``options["overwrite"]`` is true: any MCP client can call export_minutes, so it must not be a
    way to clobber arbitrary files. The default location is narumi's own export directory and is
    replaced freely. A directory at the target is always rejected.
    """
    unknown = sorted(set(options) - set(PATH_OPTIONS_SCHEMA["properties"]))
    if unknown:
        raise InvalidArgumentError(
            f"unknown export options: {', '.join(unknown)}", details={"unknown": unknown}
        )
    overwrite = options.get(OVERWRITE_OPTION, False)
    if not isinstance(overwrite, bool):
        raise InvalidArgumentError("options.overwrite must be a boolean")
    raw = options.get(OUTPUT_PATH_OPTION)
    if raw is not None:
        if not isinstance(raw, str) or not raw.strip():
            raise InvalidArgumentError("options.output_path must be a non-empty string")
        dest = Path(raw)
        if not dest.is_absolute():
            raise InvalidArgumentError(
                "options.output_path must be an absolute path", details={"output_path": raw}
            )
    else:
        dest = data_root() / "exports" / f"{bundle.meeting_id}-v{minutes_version}{suffix}"
        overwrite = True
    if dest.is_dir():
        raise InvalidArgumentError(
            f"export target is a directory: {dest}", details={"output_path": str(dest)}
        )
    if dest.exists() and not overwrite:
        raise InvalidArgumentError(
            f"export target already exists: {dest} (set options.overwrite=true to replace it)",
            details={"output_path": str(dest)},
        )
    dest.parent.mkdir(parents=True, exist_ok=True)
    return FileTarget(path=dest, overwrite=overwrite)


def copy_slides(source_dir: Path, target: FileTarget) -> tuple[str, Path | None]:
    """Copy ``<minutes dir>/slides`` next to the target as ``<stem>-slides`` and rewrite links.

    Returns the markdown text (with ``slides/`` links rewritten) and the copied directory. An
    existing ``<stem>-slides`` is replaced only when the target allows overwriting; it is never
    removed otherwise.
    """
    text = (source_dir / "minutes.md").read_text(encoding="utf-8")
    slides = source_dir / SLIDES_DIR
    if not slides.is_dir():
        return text, None
    dest = target.slides_dir
    if dest.exists():
        if not target.overwrite or not dest.is_dir():
            raise InvalidArgumentError(
                f"slides target already exists: {dest} (set options.overwrite=true to replace it)",
                details={"slides": str(dest)},
            )
        shutil.rmtree(dest)
    shutil.copytree(slides, dest)
    return text.replace(f"]({SLIDES_DIR}/", f"]({dest.name}/"), dest
