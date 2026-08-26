"""Layer 1: speaker turns derived from track separation (mic → ``me``, system → ``other``)."""

from __future__ import annotations

from narumi.diarize.base import LAYER1_ENGINE_NAME, LAYER1_ENGINE_VERSION, LAYER_TRACKS
from narumi.errors import InvalidArgumentError
from narumi.models import SPEAKER_ME, SPEAKER_OTHER, Diarization, EngineInfo, Transcript, Turn


def track_speaker(track: str | None) -> str:
    return SPEAKER_ME if track == "mic" else SPEAKER_OTHER


def build_layer1(transcripts: list[Transcript]) -> Diarization:
    """One layer-1 turn per own-transcript segment, shifted by the transcript ``time_offset``.

    Turns are sorted by ``(start, end, speaker, source_id)`` so the output is independent of
    the order in which transcripts are passed.
    """
    turns: list[Turn] = []
    for transcript in transcripts:
        if transcript.kind != "own" or transcript.track is None:
            raise InvalidArgumentError(
                "layer-1 diarization takes own transcripts with a track only",
                details={"source_id": transcript.source_id, "kind": transcript.kind},
            )
        speaker = track_speaker(transcript.track)
        for segment in transcript.segments:
            turns.append(
                Turn(
                    start=round(segment.start + transcript.time_offset, 3),
                    end=round(segment.end + transcript.time_offset, 3),
                    speaker=speaker,
                    confidence=1.0,
                    layer=LAYER_TRACKS,
                    source_id=transcript.source_id,
                )
            )
    turns.sort(key=lambda turn: (turn.start, turn.end, turn.speaker, turn.source_id or ""))
    return Diarization(
        layer=LAYER_TRACKS,
        engine=EngineInfo(name=LAYER1_ENGINE_NAME, version=LAYER1_ENGINE_VERSION),
        turns=turns,
    )
