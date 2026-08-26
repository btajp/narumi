"""``pyannote`` diarization engine (local, needs ``pyannote.audio`` + torch and a HF token).

The package is imported lazily inside ``diarize``; construction only verifies that the package
is installed and that a Hugging Face token is present, so misconfiguration fails fast with an
``EngineUnavailableError`` that says what to install / set. Nothing is ever downloaded in tests.

NOTE: the ``Pipeline.from_pretrained`` token keyword differs between pyannote.audio 3.x
(``use_auth_token``) and 4.x (``token``) and the 4.x call returns a ``DiarizeOutput`` whose
``speaker_diarization`` attribute is the ``Annotation``; both shapes are handled by introspection
because the package is not installed in the dev environment.
"""

from __future__ import annotations

import inspect
import os
from pathlib import Path
from typing import Any

from narumi.diarize.base import LAYER_ENGINE, DiarizationProfile
from narumi.errors import EngineUnavailableError
from narumi.models import Turn
from narumi.transcribe.base import package_version

DISTRIBUTION = "pyannote.audio"
ENV_MODEL = "NARUMI_PYANNOTE_MODEL"
DEFAULT_MODEL = "pyannote/speaker-diarization-3.1"
TOKEN_ENVS: tuple[str, ...] = ("HF_TOKEN", "HUGGINGFACE_TOKEN")
INSTALL_HINT = "install with `uv sync --extra pyannote` (pyannote.audio + torch)"
TOKEN_HINT = (
    "set HF_TOKEN (or HUGGINGFACE_TOKEN) to a Hugging Face token after accepting the user "
    f"conditions of https://huggingface.co/{DEFAULT_MODEL}"
)
SEED = 0


def resolve_token() -> str:
    for env in TOKEN_ENVS:
        value = os.environ.get(env)
        if value:
            return value
    raise EngineUnavailableError(
        f"pyannote needs a Hugging Face token; {TOKEN_HINT}",
        details={"engine": "pyannote", "env": list(TOKEN_ENVS)},
    )


class PyannoteEngine:
    name = "pyannote"
    profile = DiarizationProfile(sends_audio_externally=False)

    def __init__(self, model: str | None = None) -> None:
        self.model = model or os.environ.get(ENV_MODEL) or DEFAULT_MODEL
        self.version = package_version(DISTRIBUTION, install_hint=INSTALL_HINT)
        self._token = resolve_token()
        self.params: dict[str, Any] = {"model": self.model, "seed": SEED}
        self._pipeline: Any | None = None

    def _load_pipeline(self) -> Any:
        if self._pipeline is not None:
            return self._pipeline
        try:
            import torch
            from pyannote.audio import Pipeline
        except ImportError as exc:
            raise EngineUnavailableError(
                f"pyannote.audio could not be imported ({exc}); {INSTALL_HINT}",
                details={"engine": self.name, "install": INSTALL_HINT},
            ) from exc
        torch.manual_seed(SEED)
        parameters = inspect.signature(Pipeline.from_pretrained).parameters
        token_kw = "token" if "token" in parameters else "use_auth_token"
        pipeline = Pipeline.from_pretrained(self.model, **{token_kw: self._token})
        if pipeline is None:
            raise EngineUnavailableError(
                f"pyannote could not load {self.model!r}; {TOKEN_HINT}",
                details={"engine": self.name, "model": self.model},
            )
        self._pipeline = pipeline
        return pipeline

    def diarize(self, wav: Path, *, num_speakers: int | None = None) -> list[Turn]:
        pipeline = self._load_pipeline()
        kwargs: dict[str, Any] = {}
        if num_speakers is not None:
            kwargs["num_speakers"] = num_speakers
        result = pipeline(str(wav), **kwargs)
        annotation = getattr(result, "speaker_diarization", result)
        turns = [
            Turn(
                start=round(float(segment.start), 3),
                end=round(float(segment.end), 3),
                speaker=str(label),
                confidence=1.0,
                layer=LAYER_ENGINE,
            )
            for segment, _track, label in annotation.itertracks(yield_label=True)
        ]
        turns.sort(key=lambda turn: (turn.start, turn.end, turn.speaker))
        return turns
