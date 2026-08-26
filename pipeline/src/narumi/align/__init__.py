"""Stage 1: deterministic alignment of transcript sources (no LLM)."""

from narumi.align.anchors import estimate_offset, find_anchors
from narumi.align.intervals import build_intervals
from narumi.align.normalize import char_ngrams, normalize_text
from narumi.align.pipeline_stage import (
    ALIGNMENT_KEY,
    ALIGNMENT_PATH,
    build_alignment,
    choose_reference,
    load_alignment,
    load_transcripts,
    run_align,
    transcript_artifact_keys,
)

__all__ = [
    "ALIGNMENT_KEY",
    "ALIGNMENT_PATH",
    "build_alignment",
    "build_intervals",
    "char_ngrams",
    "choose_reference",
    "estimate_offset",
    "find_anchors",
    "load_alignment",
    "load_transcripts",
    "normalize_text",
    "run_align",
    "transcript_artifact_keys",
]
