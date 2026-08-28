"""``get_server_info`` / ``list_export_destinations`` / ``rebuild_catalog``."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from narumi.diarize import available_engines as diarization_engines
from narumi.errors import NarumiError
from narumi.export import list_exporters
from narumi.llm import available_providers
from narumi.preprocess import ffmpeg_path, ffprobe_path, tool_version
from narumi.transcribe import AUTO
from narumi.transcribe import available_engines as transcription_engines

from narumi_server import __version__
from narumi_server.handlers.common import jsonable
from narumi_server.recording import CHECK_CACHE_SECONDS

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
    permission_snapshot = ctx.recorder.permission_snapshot(
        max_age=0.0 if args.get("refresh_permissions", False) else CHECK_CACHE_SECONDS
    )
    permissions = permission_snapshot.permissions
    capabilities: dict[str, Any] = {
        "recording": permissions is not None and permissions["microphone"] != "denied",
        "permission_setup_in_progress": permission_snapshot.in_progress,
        "transports": list(ctx.transports),
        "workflow": {
            "provider_connections": "streamable-http" in ctx.transports,
            "provider_models": "streamable-http" in ctx.transports,
            "stage_model_selection": False,
            "ensemble_generation": False,
        },
        **capability_names(),
    }
    if permissions is not None:
        capabilities["permissions"] = permissions
    return {
        "name": SERVER_NAME,
        "server_version": __version__,
        "server_instance_id": ctx.server_instance_id,
        "contract_version": ctx.contracts.contract_version,
        "capabilities": capabilities,
        "secure_transport": {
            "mode": (
                "pinned_tls"
                if "streamable-http" in ctx.transports
                else "stdio"
                if "stdio" in ctx.transports
                else "unavailable"
            ),
            "tls_required": "streamable-http" in ctx.transports,
            "client_auth_required": "streamable-http" in ctx.transports,
        },
        "diagnostics": diagnostics(ctx),
    }


def diagnostics(ctx: ServerContext) -> dict[str, Any]:
    """Local-environment report for the app's diagnostics screen (contract ``diagnostics``)."""
    recorder = ctx.recorder.recorder_path
    return {
        "ffmpeg": _binary_diagnostic(ffmpeg_path),
        "ffprobe": _binary_diagnostic(ffprobe_path),
        "data_root": str(ctx.data_root),
        "meetings_root": str(ctx.meetings_root),
        "catalog_path": str(ctx.catalog.db_path),
        "recorder_path": None if recorder is None else str(recorder),
        "contracts_dir": str(ctx.contracts.path),
    }


def _binary_diagnostic(resolve: Callable[[], Path]) -> dict[str, str] | None:
    """``{"path", "version"}`` of a resolved binary; ``None`` when missing or unusable.

    ``None`` is the contract's way of saying "not found" on a diagnostics screen — the stages
    that actually need the binary still fail loudly with ``engine_unavailable``.
    """
    try:
        path = resolve()
        return {"path": str(path), "version": tool_version(str(path))}
    except NarumiError:
        return None


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


def rebuild_catalog(ctx: ServerContext, args: dict[str, Any]) -> dict[str, Any]:
    """Rebuild the derived catalog tables from the bundles on disk.

    ``jobs`` / ``requests`` / ``audit_log`` are preserved (``Catalog.rebuild``); per-bundle
    failures are reported in ``errors`` and never fail the tool.
    """
    stats = ctx.catalog.rebuild(ctx.meetings_root, actor=ctx.actor)
    return {
        "meetings": stats.meetings,
        "segments": stats.segments,
        "errors": list(stats.errors),
    }
