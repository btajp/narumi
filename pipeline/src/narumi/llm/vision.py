"""Vision-call helpers for screen-reading stages (layer 3).

Prompt templates live in ``narumi/slides/prompts/`` and follow the same fixed-template +
``{{placeholder}}`` contract as :mod:`narumi.generate.prompts` (kept separate because that
loader is bound to ``generate/prompts`` and caches by name only).
"""

from __future__ import annotations

import json
import re
from functools import cache
from pathlib import Path

from narumi.errors import ErrorCode, InvalidArgumentError, NarumiError, NotFoundError
from narumi.llm.base import LLMProvider

SLIDES_PROMPTS_DIR = Path(__file__).resolve().parents[1] / "slides" / "prompts"

_PLACEHOLDER_RE = re.compile(r"\{\{(\w+)\}\}")
_FENCE_RE = re.compile(r"^```[A-Za-z0-9_-]*\n(.*?)\n?```\s*$", re.DOTALL)
_ANSWER_HEAD_CHARS = 200


@cache
def load_vision_prompt(name: str) -> str:
    """Raw template ``slides/prompts/<name>.md`` (cached; templates never change at runtime)."""
    path = SLIDES_PROMPTS_DIR / f"{name}.md"
    if not path.exists():
        raise NarumiError(f"prompt template missing: {path}", details={"name": name})
    return path.read_text(encoding="utf-8")


def render_vision_prompt(name: str, **values: object) -> str:
    """Substitute every ``{{key}}`` in the template; unknown / leftover keys are an error."""
    template = load_vision_prompt(name)
    expected = set(_PLACEHOLDER_RE.findall(template))
    given = set(values)
    if expected != given:
        raise NarumiError(
            f"prompt {name}: placeholders {sorted(expected)} != values {sorted(given)}",
            details={"name": name, "missing": sorted(expected - given)},
        )
    return _PLACEHOLDER_RE.sub(lambda m: str(values[m.group(1)]), template)


def vision_complete(
    provider: LLMProvider,
    prompt: str,
    *,
    images: list[Path],
    system: str | None = None,
    max_tokens: int | None = None,
) -> str:
    """One vision completion. The provider must declare ``profile.vision``; images must exist."""
    if not images:
        raise InvalidArgumentError("vision_complete needs at least one image")
    if not provider.profile.vision:
        raise InvalidArgumentError(
            f"provider {provider.name!r} has no vision capability",
            details={"provider": provider.name},
        )
    missing = [str(p) for p in images if not Path(p).is_file()]
    if missing:
        raise NotFoundError("vision image files missing", details={"missing": missing})
    return provider.complete(
        prompt, system=system, images=[Path(p) for p in images], max_tokens=max_tokens
    )


def parse_json_answer(text: str) -> object:
    """Strictly parse an LLM answer as JSON; a single surrounding code fence is tolerated.

    Anything else — prose, partial JSON, multiple documents — raises ``NarumiError``
    (code ``internal``): the model violated the fixed prompt's output contract.
    """
    candidate = text.strip()
    fenced = _FENCE_RE.match(candidate)
    if fenced:
        candidate = fenced.group(1).strip()
    try:
        return json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise NarumiError(
            "LLM answer is not valid JSON",
            code=ErrorCode.INTERNAL,
            details={"answer_head": text[:_ANSWER_HEAD_CHARS]},
        ) from exc
