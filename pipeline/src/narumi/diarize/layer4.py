"""Layer 4: speaker turns from external transcripts that carry speaker names."""

from __future__ import annotations

from pathlib import Path

from narumi.bundle import Bundle, StageResult
from narumi.errors import InvalidArgumentError
from narumi.models import Diarization, EngineInfo, Transcript, Turn

LAYER_EXTERNAL = 4
LAYER4_ENGINE_NAME = "external-transcripts"
LAYER4_ENGINE_VERSION = "1"
LAYER4_CONFIDENCE = 0.9
"""External tools print real names but run on their own clock; slightly below layer 1."""

LAYER4_KEY = "diarization/layer4"
LAYER4_OUTPUT = "diarization/layer4-external.json"
EXT_TRANSCRIPT_KEY_PREFIX = "transcripts/ext-"


def build_layer4(transcripts: list[Transcript]) -> Diarization:
    """One layer-4 turn per external segment that names its speaker.

    ``Turn.speaker`` is the *name* the external tool printed (not an anonymous label), so layer 4
    resolves identities rather than adding labels. ``source_id`` is the ext transcript the turn
    came from; consumers must shift the turn by that source's alignment offset like they do for
    layer 1. Segments without a speaker are skipped (not an error), and turns are sorted by
    ``(start, end, speaker, source_id)`` so the output is independent of input order.
    """
    turns: list[Turn] = []
    for transcript in transcripts:
        if transcript.kind != "external":
            raise InvalidArgumentError(
                "layer-4 diarization takes external transcripts only",
                details={"source_id": transcript.source_id, "kind": transcript.kind},
            )
        for segment in transcript.segments:
            speaker = (segment.speaker or "").strip()
            if not speaker:
                continue
            start = round(max(0.0, segment.start + transcript.time_offset), 3)
            turns.append(
                Turn(
                    start=start,
                    end=round(max(start, segment.end + transcript.time_offset), 3),
                    speaker=speaker,
                    confidence=LAYER4_CONFIDENCE,
                    layer=LAYER_EXTERNAL,
                    source_id=transcript.source_id,
                )
            )
    turns.sort(key=lambda turn: (turn.start, turn.end, turn.speaker, turn.source_id or ""))
    return Diarization(
        layer=LAYER_EXTERNAL,
        engine=EngineInfo(name=LAYER4_ENGINE_NAME, version=LAYER4_ENGINE_VERSION),
        turns=turns,
    )


# ---------------------------------------------------------------------------- bundle stage
def ext_transcript_keys(bundle: Bundle) -> list[str]:
    """Sorted artifact keys of the external transcripts (``transcripts/ext-*``)."""
    return sorted(k for k in bundle.manifest.artifacts if k.startswith(EXT_TRANSCRIPT_KEY_PREFIX))


def run_layer4(bundle: Bundle, *, force: bool = False) -> StageResult | None:
    """Persist layer 4 as ``diarization/layer4-external.json`` from the ext transcripts.

    Returns ``None`` — skipped, no artifact — when the bundle has no ``transcripts/ext-*``
    artifact, dropping any stale layer-4 output (``drop_layer2`` parity: integrate consumes
    every ``diarization/*`` artifact the manifest lists). Stage identity: key
    ``diarization/layer4``, inputs = every ext transcript artifact hash, params = the builder
    version. The artifact holds exactly what :func:`build_layer4` derives, so consumers that
    derive layer 4 on the fly (``integrate`` without the artifact) see the same turns.
    """
    keys = ext_transcript_keys(bundle)
    if not keys:
        drop_layer4(bundle)
        return None
    transcripts: list[Transcript] = []
    inputs: dict[str, str] = {}
    for key in keys:
        record = bundle.artifact(key)
        assert record is not None
        transcripts.append(Transcript.model_validate(bundle.read_json(record.path)))
        inputs[key] = record.sha256

    def produce(out: Path) -> None:
        bundle.write_json(LAYER4_OUTPUT, build_layer4(transcripts))

    return bundle.run_stage(
        LAYER4_KEY,
        inputs=inputs,
        params={"version": LAYER4_ENGINE_VERSION},
        producer=(LAYER4_ENGINE_NAME, LAYER4_ENGINE_VERSION),
        output=LAYER4_OUTPUT,
        fn=produce,
        force=force,
    )


def drop_layer4(bundle: Bundle) -> bool:
    """Remove a stale layer-4 artifact (every ext transcript was removed); returns whether
    anything went. Mirrors ``drop_layer2``."""
    record = bundle.manifest.artifacts.pop(LAYER4_KEY, None)
    if record is None:
        return False
    bundle.abspath(record.path).unlink(missing_ok=True)
    bundle.save()
    return True
