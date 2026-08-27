"""Helpers for embedding key slides into a minutes version.

``select_slides_for_minutes`` is pure (unit-testable without a bundle);
``copy_slides_to_minutes`` materializes the images under ``minutes/v<N>/slides/`` so a minutes
markdown can reference them relatively. Wiring these into the generate stage is the pipeline's
call-site concern.
"""

from __future__ import annotations

import shutil
from collections.abc import Sequence
from pathlib import Path

from narumi.bundle import Bundle
from narumi.errors import InvalidArgumentError, NotFoundError
from narumi.models import MergedSegment
from narumi.slides.detect import SlideEntry, load_slides

MINUTES_SLIDES_DIRNAME = "slides"
"""Directory name under ``minutes/v<N>/`` holding the embedded slide images."""


def select_slides_for_minutes(
    slides: Sequence[SlideEntry], merged_segments: Sequence[MergedSegment]
) -> list[tuple[SlideEntry, str | None]]:
    """Anchor every key slide to the merged segment it should follow in the minutes body.

    Pure and deterministic: slides are ordered by ``(start, id)``; each is paired with the id of
    the *last* segment (in document order) whose ``start`` is at or before the slide's ``start``,
    or ``None`` when the slide precedes every segment (insert before the transcript).
    """
    anchored: list[tuple[SlideEntry, str | None]] = []
    for slide in sorted(slides, key=lambda s: (s.start, s.id)):
        anchor: str | None = None
        for segment in merged_segments:
            if segment.start <= slide.start:
                anchor = segment.id
        anchored.append((slide, anchor))
    return anchored


def copy_slides_to_minutes(
    bundle: Bundle, version: int, slides: Sequence[SlideEntry] | None = None
) -> dict[str, str]:
    """Copy slide images into ``minutes/v<version>/slides/`` → ``{slide_id: markdown ref}``.

    The returned refs (``slides/<id>.png``) are relative to the minutes version directory, i.e.
    usable directly as ``![...](slides/<id>.png)`` from ``minutes/v<version>/minutes.md``.
    ``slides`` defaults to every slide of the bundle (:func:`load_slides`).
    """
    if version < 1:
        raise InvalidArgumentError("minutes version must be >= 1", details={"version": version})
    entries = list(slides) if slides is not None else load_slides(bundle)
    target_dir: Path = bundle.minutes_dir(version) / MINUTES_SLIDES_DIRNAME
    target_dir.mkdir(parents=True, exist_ok=True)
    refs: dict[str, str] = {}
    for entry in entries:
        source = bundle.abspath(entry.path)
        if not source.is_file():
            raise NotFoundError(
                f"slide image missing: {entry.path}; re-run the slides stage "
                "(run_slides(force=True))",
                details={"slide": entry.id, "path": entry.path},
            )
        shutil.copyfile(source, target_dir / f"{entry.id}.png")
        refs[entry.id] = f"{MINUTES_SLIDES_DIRNAME}/{entry.id}.png"
    return refs
