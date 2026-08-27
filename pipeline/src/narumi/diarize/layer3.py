"""Layer 3: on-screen active-speaker highlight reading via a vision LLM.

For every layer-2 speaker turn boundary the nearest extracted frame (within ±2 s) is sent to
the configured vision provider with the fixed ``layer3_speakers`` prompt, which reads the name
the meeting UI highlights while that person speaks. Two files are written together:

- ``diarization/layer3-screen.json`` — a plain :class:`~narumi.models.Diarization` (layer 3,
  the hashed stage artifact, key ``diarization/layer3``) whose turns re-state the annotated
  layer-2 turns (same anonymous label) with the vision confidence;
- ``diarization/layer3-names.json`` — the name-suggestion map
  ``{label: {name, confidence, evidence}}`` for the speaker-map builder (written and removed
  with the artifact, like ``merged/speaker_map.json`` it is not separately hashed).

The stage runs only when the provider's capability profile declares vision AND the meeting's
``external_send_policy`` allows the provider. A disallowed provider raises
``PolicyViolationError`` (絶対原則 4 — never a silent skip); a provider without vision (or
``none``), or a bundle without layer-2 / key-slide artifacts, skips with **no artifact**
(returns ``None``) and drops any stale layer-3 output so downstream inputs stay honest.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from narumi.bundle import Bundle, StageResult
from narumi.diarize.stage import LAYER2_KEY
from narumi.errors import ErrorCode, InvalidArgumentError, NarumiError, NotFoundError
from narumi.llm.policy import check_policy
from narumi.llm.registry import get_provider, provider_profile
from narumi.llm.vision import parse_json_answer, render_vision_prompt, vision_complete
from narumi.models import Diarization, EngineInfo, Turn
from narumi.slides.detect import SLIDES_KEY, frame_time_sec, list_frames

LAYER_SCREEN = 3
LAYER3_KEY = "diarization/layer3"
LAYER3_OUTPUT = "diarization/layer3-screen.json"
LAYER3_NAMES_PATH = "diarization/layer3-names.json"
LAYER3_PROMPT_NAME = "layer3_speakers"
LAYER3_PROMPT_VERSION = "layer3-v1"
LAYER3_SYSTEM_PROMPT = (
    "あなたは会議画面のスクリーンショットから発話中の参加者名を読み取るアシスタントです。"
    "指示された JSON 形式でのみ出力してください。"
)
BOUNDARY_WINDOW_SEC = 2.0
"""A turn boundary uses the nearest frame at most this many seconds away."""
BATCH_SIZE = 4
"""Images per provider call."""
ENGINE_NAME = "screen-vision"
ENGINE_VERSION = "1"

_ANSWER_KEYS = {"image", "name", "confidence", "evidence"}


class NameSuggestion(BaseModel):
    """One entry of ``diarization/layer3-names.json``: screen evidence for a label's name."""

    model_config = ConfigDict(extra="forbid")

    name: str
    confidence: float = Field(ge=0, le=1)
    evidence: str


@dataclass(frozen=True)
class BoundarySample:
    """One layer-2 turn boundary paired with the frame that will be shown to the provider."""

    turn_index: int
    label: str
    time: float
    frame: Path


def boundary_samples(
    turns: Sequence[Turn], frames: Sequence[Path], *, window: float = BOUNDARY_WINDOW_SEC
) -> list[BoundarySample]:
    """One sample per turn start that has a frame within ``±window`` seconds (else dropped)."""
    if window < 0:
        raise InvalidArgumentError("window must be >= 0", details={"value": window})
    times = sorted((frame_time_sec(frame), frame) for frame in frames)
    samples: list[BoundarySample] = []
    for index, turn in enumerate(turns):
        best = min(times, key=lambda tf: (abs(tf[0] - turn.start), tf[0]), default=None)
        if best is not None and abs(best[0] - turn.start) <= window:
            samples.append(
                BoundarySample(turn_index=index, label=turn.speaker, time=best[0], frame=best[1])
            )
    return samples


def validate_layer3_answer(data: object, count: int) -> list[dict[str, object]]:
    """jsonschema-lite validation of the provider's JSON answer (strict; no coercion).

    The answer must be an array of ``{image, name, confidence, evidence}`` objects with
    ``image`` a unique integer in ``1..count``, ``name`` null or a non-empty string,
    ``confidence`` a number in ``[0, 1]`` and ``evidence`` a string. Images the model omits
    count as "not identified"; everything else raises ``NarumiError`` (code ``internal``).
    """

    def fail(reason: str, **details: object) -> NarumiError:
        return NarumiError(
            f"layer-3 vision answer rejected: {reason}",
            code=ErrorCode.INTERNAL,
            details={"reason": reason, **details},
        )

    if not isinstance(data, list):
        raise fail("answer is not a JSON array", type=type(data).__name__)
    items: list[dict[str, object]] = []
    seen: set[int] = set()
    for index, item in enumerate(data):
        if not isinstance(item, dict):
            raise fail("array element is not an object", index=index)
        if set(item) != _ANSWER_KEYS:
            raise fail("unexpected keys", index=index, keys=sorted(item))
        image = item["image"]
        if isinstance(image, bool) or not isinstance(image, int) or not 1 <= image <= count:
            raise fail(
                "image must be an integer in 1..count", index=index, image=image, count=count
            )
        if image in seen:
            raise fail("duplicate image index", index=index, image=image)
        seen.add(image)
        name = item["name"]
        if name is not None and (not isinstance(name, str) or not name.strip()):
            raise fail("name must be null or a non-empty string", index=index, name=name)
        confidence = item["confidence"]
        if (
            isinstance(confidence, bool)
            or not isinstance(confidence, int | float)
            or not 0 <= confidence <= 1
        ):
            raise fail("confidence must be a number in [0, 1]", index=index, confidence=confidence)
        if not isinstance(item["evidence"], str):
            raise fail("evidence must be a string", index=index)
        items.append(item)
    return items


def load_layer3_names(bundle: Bundle) -> dict[str, NameSuggestion]:
    """Name suggestions of the layer-3 artifact (``{}`` when the stage never ran)."""
    if bundle.artifact(LAYER3_KEY) is None:
        return {}
    data = bundle.read_json(LAYER3_NAMES_PATH)
    return {label: NameSuggestion.model_validate(entry) for label, entry in data.items()}


def drop_layer3(bundle: Bundle) -> bool:
    """Remove a stale layer-3 artifact (and its names file); returns whether anything went.

    Mirrors ``drop_layer2``: integrate consumes every ``diarization/*`` artifact the manifest
    lists, so output the current config can no longer produce must not linger.
    """
    record = bundle.manifest.artifacts.pop(LAYER3_KEY, None)
    if record is None:
        return False
    bundle.abspath(record.path).unlink(missing_ok=True)
    bundle.abspath(LAYER3_NAMES_PATH).unlink(missing_ok=True)
    bundle.save()
    return True


def run_layer3(bundle: Bundle, *, force: bool = False) -> StageResult | None:
    """Run layer 3 idempotently; ``None`` = skipped with no artifact (see module docstring).

    Stage identity: key ``diarization/layer3``, inputs = layer-2 + key-slide artifact hashes,
    params = provider / prompt_version / window / batch_size.
    """
    config = bundle.manifest.config
    name = config.llm_provider
    profile = provider_profile(name)
    if not profile.vision:
        drop_layer3(bundle)
        return None
    check_policy(profile, config.external_send_policy, provider=name)
    layer2 = bundle.artifact(LAYER2_KEY)
    slides = bundle.artifact(SLIDES_KEY)
    if layer2 is None or slides is None:
        drop_layer3(bundle)
        return None
    diarization = Diarization.model_validate(bundle.read_json(layer2.path))
    inputs = {LAYER2_KEY: layer2.sha256, SLIDES_KEY: slides.sha256}
    params = {
        "provider": name,
        "prompt_version": LAYER3_PROMPT_VERSION,
        "window": BOUNDARY_WINDOW_SEC,
        "batch_size": BATCH_SIZE,
    }

    def produce(out: Path) -> None:
        frames = list_frames(bundle)
        if not frames:
            raise NotFoundError(
                "preprocess/frames is empty; re-run the slides stage (run_slides(force=True))",
                details={"meeting_id": bundle.meeting_id},
            )
        samples = boundary_samples(diarization.turns, frames, window=BOUNDARY_WINDOW_SEC)
        results = _query_provider(get_provider(name), samples) if samples else []
        turns, suggestions = _build_outputs(diarization.turns, samples, results)
        engine = EngineInfo(name=ENGINE_NAME, version=ENGINE_VERSION, params=dict(params))
        bundle.write_json(
            LAYER3_OUTPUT, Diarization(layer=LAYER_SCREEN, engine=engine, turns=turns)
        )
        bundle.write_json(
            LAYER3_NAMES_PATH, {label: entry.model_dump() for label, entry in suggestions.items()}
        )

    return bundle.run_stage(
        LAYER3_KEY,
        inputs=inputs,
        params=params,
        producer=(ENGINE_NAME, ENGINE_VERSION),
        output=LAYER3_OUTPUT,
        fn=produce,
        force=force,
    )


# ---------------------------------------------------------------------------- internals
def _chunked(samples: Sequence[BoundarySample], size: int) -> Iterator[Sequence[BoundarySample]]:
    for start in range(0, len(samples), size):
        yield samples[start : start + size]


def _query_provider(provider, samples: Sequence[BoundarySample]) -> list[dict[str, object] | None]:
    """Ask the provider about every sample in fixed-size batches → one result per sample."""
    results: list[dict[str, object] | None] = []
    for batch in _chunked(samples, BATCH_SIZE):
        items = "\n".join(
            f"- 画像 {i}: 時刻 {_clock(sample.time)}" for i, sample in enumerate(batch, start=1)
        )
        prompt = render_vision_prompt(LAYER3_PROMPT_NAME, count=len(batch), items=items)
        answer = vision_complete(
            provider,
            prompt,
            images=[sample.frame for sample in batch],
            system=LAYER3_SYSTEM_PROMPT,
        )
        validated = validate_layer3_answer(parse_json_answer(answer), len(batch))
        by_image: dict[int, dict[str, object]] = {int(item["image"]): item for item in validated}  # type: ignore[call-overload]
        results.extend(by_image.get(i) for i in range(1, len(batch) + 1))
    return results


def _build_outputs(
    turns: Sequence[Turn],
    samples: Sequence[BoundarySample],
    results: Sequence[dict[str, object] | None],
) -> tuple[list[Turn], dict[str, NameSuggestion]]:
    """Aggregate per-image identifications into layer-3 turns + a name-suggestion map."""
    turn_confidence: dict[int, float] = {}
    votes: dict[str, dict[str, list[tuple[float, float, str]]]] = {}
    for sample, item in zip(samples, results, strict=True):
        if item is None or item["name"] is None:
            continue
        name = str(item["name"]).strip()
        confidence = float(item["confidence"])  # type: ignore[arg-type]
        evidence = str(item["evidence"]).strip()
        previous = turn_confidence.get(sample.turn_index, 0.0)
        turn_confidence[sample.turn_index] = max(previous, confidence)
        votes.setdefault(sample.label, {}).setdefault(name, []).append(
            (sample.time, confidence, evidence)
        )
    layer_turns = [
        Turn(
            start=turns[index].start,
            end=turns[index].end,
            speaker=turns[index].speaker,
            confidence=round(confidence, 3),
            layer=LAYER_SCREEN,
        )
        for index, confidence in sorted(turn_confidence.items())
    ]
    suggestions: dict[str, NameSuggestion] = {}
    for label in sorted(votes):
        by_name = votes[label]
        best = max(by_name, key=lambda n: (sum(c for _, c, _ in by_name[n]), n))
        entries = sorted(by_name[best])
        suggestions[label] = NameSuggestion(
            name=best,
            confidence=round(max(c for _, c, _ in entries), 3),
            evidence="; ".join(f"{_clock(t)} {ev}".rstrip() for t, _, ev in entries),
        )
    return layer_turns, suggestions


def _clock(seconds: float) -> str:
    total = int(seconds)
    return f"{total // 3600:02d}:{total % 3600 // 60:02d}:{total % 60:02d}"
