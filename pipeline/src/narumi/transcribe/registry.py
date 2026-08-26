"""Transcription engine registry: ``fake`` / ``mlx-whisper`` / ``faster-whisper`` / ``auto``.

Availability is decided with ``importlib.util.find_spec`` (cheap; the whisper packages take
10-20 s to actually import), and engines import their packages lazily in ``transcribe``.
"""

from __future__ import annotations

import importlib.util
from collections.abc import Callable

from narumi.errors import EngineUnavailableError, InvalidArgumentError
from narumi.transcribe.base import EngineProfile, TranscriptionEngine
from narumi.transcribe.fake import FakeEngine
from narumi.transcribe.faster_whisper_engine import FasterWhisperEngine
from narumi.transcribe.mlx_whisper_engine import MlxWhisperEngine

AUTO = "auto"

ENGINE_FACTORIES: dict[str, Callable[[], TranscriptionEngine]] = {
    FakeEngine.name: FakeEngine,
    MlxWhisperEngine.name: MlxWhisperEngine,
    FasterWhisperEngine.name: FasterWhisperEngine,
}

ENGINE_MODULES: dict[str, str] = {
    MlxWhisperEngine.name: "mlx_whisper",
    FasterWhisperEngine.name: "faster_whisper",
}
"""Import name whose presence makes the engine available (engines without an entry always are)."""

INSTALL_HINTS: dict[str, str] = {
    MlxWhisperEngine.name: "uv sync --extra whisper-mlx",
    FasterWhisperEngine.name: "uv sync --extra whisper-faster",
}

AUTO_ORDER: tuple[str, ...] = (MlxWhisperEngine.name, FasterWhisperEngine.name)


def _module_available(module: str) -> bool:
    """``True`` when ``module`` can be located without importing it."""
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        # ImportError: a parent package is missing; ValueError: a stale module spec.
        return False


def is_engine_available(name: str) -> bool:
    if name not in ENGINE_FACTORIES:
        return False
    module = ENGINE_MODULES.get(name)
    return module is None or _module_available(module)


def available_engines() -> list[str]:
    """Registry names whose package can be located (always includes ``fake``)."""
    return [name for name in ENGINE_FACTORIES if is_engine_available(name)]


def resolve_engine_name(name: str) -> str:
    """Turn ``auto`` into a concrete registry name; validate explicit names."""
    if not name:
        raise InvalidArgumentError("transcription engine name is empty")
    if name == AUTO:
        for candidate in AUTO_ORDER:
            if is_engine_available(candidate):
                return candidate
        hints = {engine: INSTALL_HINTS[engine] for engine in AUTO_ORDER}
        raise EngineUnavailableError(
            "no local Whisper engine is installed for transcription_engine='auto'; install one of "
            + ", ".join(f"{engine} (`{hint}`)" for engine, hint in hints.items())
            + ", or set transcription_engine explicitly (e.g. 'fake' for tests)",
            details={"tried": list(AUTO_ORDER), "install": hints},
        )
    if name not in ENGINE_FACTORIES:
        raise InvalidArgumentError(
            f"unknown transcription engine {name!r}; known: "
            + ", ".join([AUTO, *ENGINE_FACTORIES]),
            details={"engine": name, "known": [AUTO, *ENGINE_FACTORIES]},
        )
    if not is_engine_available(name):
        raise EngineUnavailableError(
            f"transcription engine {name!r} is not installed; `{INSTALL_HINTS[name]}`",
            details={"engine": name, "install": INSTALL_HINTS[name]},
        )
    return name


def get_engine(name: str) -> TranscriptionEngine:
    """Instantiate the engine for ``name`` (``auto`` → first of ``AUTO_ORDER`` that is installed).

    Construction never loads a model or imports a heavy package.
    """
    return ENGINE_FACTORIES[resolve_engine_name(name)]()


def engine_profile(name: str) -> EngineProfile:
    return get_engine(name).profile
