"""Diarization engine registry: ``none`` / ``fake`` / ``pyannote``."""

from __future__ import annotations

import importlib.util
from collections.abc import Callable

from narumi.diarize.base import DiarizationEngine, DiarizationProfile
from narumi.diarize.builtin import FakeDiarizationEngine, NoneEngine
from narumi.diarize.pyannote_engine import PyannoteEngine
from narumi.errors import EngineUnavailableError, InvalidArgumentError

ENGINE_FACTORIES: dict[str, Callable[[], DiarizationEngine]] = {
    NoneEngine.name: NoneEngine,
    FakeDiarizationEngine.name: FakeDiarizationEngine,
    PyannoteEngine.name: PyannoteEngine,
}

ENGINE_MODULES: dict[str, str] = {PyannoteEngine.name: "pyannote.audio"}
INSTALL_HINTS: dict[str, str] = {
    PyannoteEngine.name: "uv sync --extra pyannote (then set HF_TOKEN)",
}


def _module_available(module: str) -> bool:
    """``True`` when ``module`` can be located without importing it (torch is heavy)."""
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def is_engine_available(name: str) -> bool:
    if name not in ENGINE_FACTORIES:
        return False
    module = ENGINE_MODULES.get(name)
    return module is None or _module_available(module)


def available_engines() -> list[str]:
    """Registry names whose package can be located (always includes ``none`` and ``fake``)."""
    return [name for name in ENGINE_FACTORIES if is_engine_available(name)]


def get_engine(name: str) -> DiarizationEngine:
    """Instantiate the diarization engine ``name``; never loads a model.

    Unknown names raise ``InvalidArgumentError``; a known engine whose package (or token) is
    missing raises ``EngineUnavailableError`` with install / token instructions.
    """
    if not name:
        raise InvalidArgumentError("diarization engine name is empty")
    if name not in ENGINE_FACTORIES:
        raise InvalidArgumentError(
            f"unknown diarization engine {name!r}; known: " + ", ".join(ENGINE_FACTORIES),
            details={"engine": name, "known": list(ENGINE_FACTORIES)},
        )
    if not is_engine_available(name):
        raise EngineUnavailableError(
            f"diarization engine {name!r} is not installed; `{INSTALL_HINTS[name]}`",
            details={"engine": name, "install": INSTALL_HINTS[name]},
        )
    return ENGINE_FACTORIES[name]()


def engine_profile(name: str) -> DiarizationProfile:
    return get_engine(name).profile
