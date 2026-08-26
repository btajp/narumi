"""Stage 2 (interval integration) and minutes generation."""

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
    MINUTES_PROMPT_VERSION,
    PLAIN_PLACEHOLDER,
    generate_minutes,
    minutes_key,
    minutes_output,
    run_generate,
)
from narumi.generate.prompts import load_prompt, render_prompt

__all__ = [
    "INTEGRATE_KEY",
    "INTEGRATE_PATH",
    "INTEGRATE_PROMPT_VERSION",
    "MINUTES_PROMPT_VERSION",
    "PLAIN_PLACEHOLDER",
    "SPEAKER_MAP_PATH",
    "build_speaker_map",
    "generate_minutes",
    "integrate",
    "load_diarizations",
    "load_merged",
    "load_prompt",
    "minutes_key",
    "minutes_output",
    "render_prompt",
    "run_generate",
    "run_integrate",
]
