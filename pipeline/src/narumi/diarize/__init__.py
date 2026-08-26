"""Speaker diarization: layer 1 (tracks), layer 2 engines (none / fake / pyannote), assignment."""

from narumi.diarize.assign import assign_speakers
from narumi.diarize.base import (
    LAYER_ENGINE,
    LAYER_TRACKS,
    DiarizationEngine,
    DiarizationProfile,
    speaker_label,
)
from narumi.diarize.builtin import FakeDiarizationEngine, NoneEngine
from narumi.diarize.builtin import sidecar_path as fake_sidecar_path
from narumi.diarize.layer1 import build_layer1, track_speaker
from narumi.diarize.pyannote_engine import PyannoteEngine
from narumi.diarize.registry import (
    ENGINE_FACTORIES,
    ENGINE_MODULES,
    available_engines,
    engine_profile,
    get_engine,
    is_engine_available,
)
from narumi.diarize.stage import (
    LAYER1_KEY,
    LAYER1_OUTPUT,
    LAYER2_KEY,
    diarize_layer2,
    drop_layer2,
    layer2_output_path,
    load_own_transcripts,
    run_diarize,
)

__all__ = [
    "ENGINE_FACTORIES",
    "ENGINE_MODULES",
    "LAYER1_KEY",
    "LAYER1_OUTPUT",
    "LAYER2_KEY",
    "LAYER_ENGINE",
    "LAYER_TRACKS",
    "DiarizationEngine",
    "DiarizationProfile",
    "FakeDiarizationEngine",
    "NoneEngine",
    "PyannoteEngine",
    "assign_speakers",
    "available_engines",
    "build_layer1",
    "diarize_layer2",
    "drop_layer2",
    "engine_profile",
    "fake_sidecar_path",
    "get_engine",
    "is_engine_available",
    "layer2_output_path",
    "load_own_transcripts",
    "run_diarize",
    "speaker_label",
    "track_speaker",
]
