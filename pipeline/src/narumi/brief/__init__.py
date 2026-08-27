"""Meeting brief v1 (コンテキスト注入): local bundle data + optional gaia-library."""

from narumi.brief.builder import (
    BRIEF_ARTIFACT_KEY,
    BRIEF_PATH,
    Brief,
    BriefSource,
    Participant,
    build_brief,
    inject_brief,
    load_brief,
    run_brief,
)

__all__ = [
    "BRIEF_ARTIFACT_KEY",
    "BRIEF_PATH",
    "Brief",
    "BriefSource",
    "Participant",
    "build_brief",
    "inject_brief",
    "load_brief",
    "run_brief",
]
