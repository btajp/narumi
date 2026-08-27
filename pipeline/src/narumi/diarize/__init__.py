"""Speaker diarization: layer 1 (tracks), layer 2 engines (none / fake / pyannote), layer 3
(screen vision), layer 4 (external transcript names), assignment."""

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
from narumi.diarize.layer3 import (
    LAYER3_KEY,
    LAYER3_NAMES_PATH,
    LAYER3_OUTPUT,
    LAYER_SCREEN,
    NameSuggestion,
    drop_layer3,
    load_layer3_names,
    run_layer3,
)
from narumi.diarize.layer4 import (
    LAYER4_KEY,
    LAYER4_OUTPUT,
    LAYER_EXTERNAL,
    build_layer4,
    drop_layer4,
    ext_transcript_keys,
    run_layer4,
)
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
    "LAYER3_KEY",
    "LAYER3_NAMES_PATH",
    "LAYER3_OUTPUT",
    "LAYER4_KEY",
    "LAYER4_OUTPUT",
    "LAYER_ENGINE",
    "LAYER_EXTERNAL",
    "LAYER_SCREEN",
    "LAYER_TRACKS",
    "DiarizationEngine",
    "DiarizationProfile",
    "FakeDiarizationEngine",
    "NameSuggestion",
    "NoneEngine",
    "PyannoteEngine",
    "assign_speakers",
    "available_engines",
    "build_layer1",
    "build_layer4",
    "diarize_layer2",
    "drop_layer2",
    "drop_layer3",
    "drop_layer4",
    "engine_profile",
    "ext_transcript_keys",
    "fake_sidecar_path",
    "get_engine",
    "is_engine_available",
    "layer2_output_path",
    "load_layer3_names",
    "load_own_transcripts",
    "run_diarize",
    "run_layer3",
    "run_layer4",
    "speaker_label",
    "track_speaker",
]
