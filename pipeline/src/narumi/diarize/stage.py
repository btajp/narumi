"""``run_diarize``: layer 1 from own transcripts, optional layer 2 from the system track."""

from __future__ import annotations

from pathlib import Path

from narumi.bundle import Bundle, StageResult
from narumi.diarize.base import (
    LAYER1_ENGINE_NAME,
    LAYER1_ENGINE_VERSION,
    LAYER_ENGINE,
    DiarizationEngine,
)
from narumi.diarize.layer1 import build_layer1
from narumi.diarize.registry import get_engine
from narumi.errors import ErrorCode, InvalidArgumentError, NarumiError
from narumi.models import Diarization, EngineInfo, Transcript, Turn
from narumi.preprocess.stage import AUDIO_TRACKS, audio_artifact_key
from narumi.transcribe.policy import check_send_policy
from narumi.transcribe.stage import transcript_artifact_key

NONE_ENGINE = "none"
LAYER2_TRACK = "system"

LAYER1_KEY = "diarization/layer1"
LAYER1_OUTPUT = "diarization/layer1-tracks.json"
LAYER2_KEY = "diarization/layer2"


def layer2_output_path(engine_name: str) -> str:
    """Bundle-relative path of the layer-2 artifact (``diarization/layer2-<engine>.json``)."""
    return f"diarization/layer2-{engine_name}.json"


def load_own_transcripts(bundle: Bundle) -> tuple[list[Transcript], dict[str, str]]:
    """Own transcripts recorded in the manifest plus their ``{artifact key: sha256}`` inputs."""
    transcripts: list[Transcript] = []
    inputs: dict[str, str] = {}
    for track in AUDIO_TRACKS:
        key = transcript_artifact_key(track)
        record = bundle.artifact(key)
        if record is None:
            continue
        path = bundle.artifact_path(key)
        transcripts.append(Transcript.model_validate_json(path.read_text(encoding="utf-8")))
        inputs[key] = record.sha256
    return transcripts, inputs


def validate_layer2_turns(turns: list[Turn], *, engine: str) -> None:
    for index, turn in enumerate(turns):
        if turn.layer != LAYER_ENGINE:
            raise NarumiError(
                f"diarization engine {engine!r} returned a layer-{turn.layer} turn at {index}",
                code=ErrorCode.INTERNAL,
                details={"engine": engine, "index": index, "layer": turn.layer},
            )


def diarize_layer2(engine: DiarizationEngine, wav: Path) -> Diarization:
    turns = engine.diarize(wav)
    validate_layer2_turns(turns, engine=engine.name)
    return Diarization(
        layer=LAYER_ENGINE,
        engine=EngineInfo(name=engine.name, version=engine.version, params=dict(engine.params)),
        turns=turns,
    )


def run_diarize(bundle: Bundle, *, force: bool = False) -> list[StageResult]:
    """Always build layer 1; run layer 2 on the system wav when ``diarization_engine != none``.

    Layer-1 inputs are the own-transcript hashes; layer-2 inputs are the ``preprocess/audio/system``
    hash (the mic track is already attributed to ``me``). The layer-2 engine is policy-checked
    before it runs.
    """
    transcripts, inputs = load_own_transcripts(bundle)
    if not transcripts:
        raise InvalidArgumentError(
            "no own transcript artifact found; run transcribe first",
            details={"expected": [transcript_artifact_key(t) for t in AUDIO_TRACKS]},
        )

    def produce_layer1(out: Path) -> None:
        bundle.write_json(LAYER1_OUTPUT, build_layer1(transcripts))

    results = [
        bundle.run_stage(
            LAYER1_KEY,
            inputs=inputs,
            params={},
            producer=(LAYER1_ENGINE_NAME, LAYER1_ENGINE_VERSION),
            output=LAYER1_OUTPUT,
            fn=produce_layer1,
            force=force,
        )
    ]

    config = bundle.manifest.config
    if config.diarization_engine == NONE_ENGINE:
        drop_layer2(bundle)
        return results
    engine = get_engine(config.diarization_engine)
    check_send_policy(
        config.external_send_policy,
        engine.profile,
        subject=f"diarization engine {engine.name!r}",
    )
    system_key = audio_artifact_key(LAYER2_TRACK)
    system_record = bundle.artifact(system_key)
    if system_record is None:
        raise InvalidArgumentError(
            f"diarization engine {engine.name!r} needs the system track wav ({system_key}) but "
            "this bundle has none; the mic track is already attributed to 'me', so set "
            "diarization_engine to 'none' for mic-only recordings",
            details={"engine": engine.name, "expected": system_key},
        )
    wav = bundle.artifact_path(system_key)
    output = layer2_output_path(engine.name)
    previous = bundle.artifact(LAYER2_KEY)

    def produce_layer2(out: Path) -> None:
        bundle.write_json(output, diarize_layer2(engine, wav))

    results.append(
        bundle.run_stage(
            LAYER2_KEY,
            inputs={system_key: system_record.sha256},
            params={
                "engine": engine.name,
                "version": engine.version,
                "params": dict(engine.params),
            },
            producer=(engine.name, engine.version),
            output=output,
            fn=produce_layer2,
            force=force,
        )
    )
    if previous is not None and previous.path != output:
        # the key was re-pointed at another engine's file; its predecessor is no longer described
        # by the manifest, so it must not linger in the bundle
        bundle.abspath(previous.path).unlink(missing_ok=True)
    return results


def drop_layer2(bundle: Bundle) -> bool:
    """Remove a layer-2 artifact left by a previously configured engine (``none`` now).

    ``integrate`` consumes every ``diarization/*`` artifact the manifest lists, so a stale layer 2
    would keep feeding speaker turns the config disabled; removing the record (and its file)
    changes integrate's inputs and makes it re-run. Returns whether anything was removed.
    """
    record = bundle.manifest.artifacts.pop(LAYER2_KEY, None)
    if record is None:
        return False
    bundle.abspath(record.path).unlink(missing_ok=True)
    bundle.save()
    return True
