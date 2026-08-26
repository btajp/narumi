"""``get_server_info`` / ``list_export_destinations``."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from narumi.diarize import available_engines as diarization_engines
from narumi.export import list_exporters
from narumi.llm import available_providers
from narumi.transcribe import AUTO
from narumi.transcribe import available_engines as transcription_engines

from narumi_server import __version__
from narumi_server.handlers.common import jsonable

if TYPE_CHECKING:
    from narumi_server.context import ServerContext

SERVER_NAME = "narumi"


def capability_names() -> dict[str, list[str]]:
    """Registry names straight from the stage packages (a broken registry fails loudly).

    ``transcription_engines`` lists ``auto`` first when at least one local Whisper engine is
    installed, because ``auto`` is a valid ``transcription_engine`` value only in that case.
    """
    transcription = list(transcription_engines())
    if any(name != "fake" for name in transcription):
        transcription.insert(0, AUTO)
    return {
        "transcription_engines": transcription,
        "diarization_engines": list(diarization_engines()),
        "llm_providers": list(available_providers()),
        "export_destinations": [str(item["name"]) for item in list_exporters()],
    }


def get_server_info(ctx: ServerContext, args: dict[str, Any]) -> dict[str, Any]:
    permissions = ctx.recorder.permissions()  # runs `narumi-recorder check` (cached briefly)
    capabilities: dict[str, Any] = {
        "recording": ctx.recorder.available(),
        "transports": list(ctx.transports),
        **capability_names(),
    }
    if permissions is not None:
        capabilities["permissions"] = permissions
    return {
        "name": SERVER_NAME,
        "server_version": __version__,
        "contract_version": ctx.contracts.contract_version,
        "capabilities": capabilities,
    }


def list_export_destinations(ctx: ServerContext, args: dict[str, Any]) -> dict[str, Any]:
    destinations: list[dict[str, Any]] = []
    for item in list_exporters():
        entry: dict[str, Any] = {
            "name": str(item["name"]),
            "description": str(item.get("description") or ""),
        }
        schema = item.get("options_schema")
        if isinstance(schema, dict):
            entry["options_schema"] = jsonable(schema)
        destinations.append(entry)
    return {"destinations": destinations}
