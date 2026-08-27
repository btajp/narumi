"""Stage 2 (interval integration) and minutes generation."""

from narumi.generate.cache import CACHE_PATH, IntegrateCache, interval_fingerprint
from narumi.generate.integrate import (
    INTEGRATE_KEY,
    INTEGRATE_PATH,
    INTEGRATE_PROMPT_VERSION,
    SPEAKER_MAP_PATH,
    build_speaker_map,
    integrate,
    load_diarizations,
    load_merged,
    run_integrate,
)
from narumi.generate.minutes import (
    LAYER3_NOTE,
    LAYER4_NOTE,
    MINUTES_PROMPT_VERSION,
    PLAIN_PLACEHOLDER,
    generate_minutes,
    minutes_key,
    minutes_output,
    run_generate,
    transcript_lines_with_slides,
)
from narumi.generate.prompts import load_prompt, render_prompt

__all__ = [
    "CACHE_PATH",
    "INTEGRATE_KEY",
    "INTEGRATE_PATH",
    "INTEGRATE_PROMPT_VERSION",
    "LAYER3_NOTE",
    "LAYER4_NOTE",
    "MINUTES_PROMPT_VERSION",
    "PLAIN_PLACEHOLDER",
    "SPEAKER_MAP_PATH",
    "IntegrateCache",
    "build_speaker_map",
    "generate_minutes",
    "integrate",
    "interval_fingerprint",
    "load_diarizations",
    "load_merged",
    "load_prompt",
    "minutes_key",
    "minutes_output",
    "render_prompt",
    "run_generate",
    "run_integrate",
    "transcript_lines_with_slides",
]
