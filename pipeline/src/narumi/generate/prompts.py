"""Fixed prompt templates (``generate/prompts/*.md``) with ``{{placeholder}}`` substitution."""

from __future__ import annotations

import re
from functools import cache
from pathlib import Path

from narumi.errors import NarumiError

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
_PLACEHOLDER_RE = re.compile(r"\{\{(\w+)\}\}")


@cache
def load_prompt(name: str) -> str:
    """Return the raw template ``prompts/<name>.md`` (cached; templates never change at runtime)."""
    path = PROMPTS_DIR / f"{name}.md"
    if not path.exists():
        raise NarumiError(f"prompt template missing: {path}", details={"name": name})
    return path.read_text(encoding="utf-8")


def render_prompt(name: str, **values: object) -> str:
    """Substitute every ``{{key}}`` in the template; unknown / leftover keys are an error."""
    template = load_prompt(name)
    expected = set(_PLACEHOLDER_RE.findall(template))
    given = set(values)
    if expected != given:
        raise NarumiError(
            f"prompt {name}: placeholders {sorted(expected)} != values {sorted(given)}",
            details={"name": name, "missing": sorted(expected - given)},
        )
    return _PLACEHOLDER_RE.sub(lambda m: str(values[m.group(1)]), template)
