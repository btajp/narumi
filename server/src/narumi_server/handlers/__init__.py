"""Tool name → handler registry. Every contract tool must have exactly one entry here."""

from __future__ import annotations

from narumi_server.handlers import (
    contexts,
    gaia,
    importing,
    lifecycle,
    meetings,
    processing,
    profiles,
    providers,
    recording,
    server_info,
)
from narumi_server.handlers.common import Handler

HANDLERS: dict[str, Handler] = {
    "get_server_info": server_info.get_server_info,
    "configure_recording_permission": recording.configure_recording_permission,
    "get_gaia_connection": gaia.get_gaia_connection,
    "set_gaia_connection": gaia.set_gaia_connection,
    "test_gaia_connection": gaia.test_gaia_connection,
    "list_providers": providers.list_providers,
    "list_provider_connections": providers.list_provider_connections,
    "set_provider_connection": providers.set_provider_connection,
    "delete_provider_connection": providers.delete_provider_connection,
    "prepare_provider_runtime": providers.prepare_provider_runtime,
    "authenticate_provider_connection": providers.authenticate_provider_connection,
    "get_provider_auth_status": providers.get_provider_auth_status,
    "test_provider_connection": providers.test_provider_connection,
    "list_provider_models": providers.list_provider_models,
    "start_recording": recording.start_recording,
    "stop_recording": recording.stop_recording,
    "get_recording_status": recording.get_recording_status,
    "import_recording": importing.import_recording,
    "list_meetings": meetings.list_meetings,
    "search_transcripts": meetings.search_transcripts,
    "get_meeting": meetings.get_meeting,
    "get_transcript": meetings.get_transcript,
    "get_minutes": meetings.get_minutes,
    "register_context": contexts.register_context,
    "regenerate": processing.regenerate,
    "set_meeting_config": meetings.set_meeting_config,
    "export_minutes": processing.export_minutes,
    "list_export_destinations": server_info.list_export_destinations,
    "get_job_status": processing.get_job_status,
    "cancel_job": processing.cancel_job,
    "discard_tracks": lifecycle.discard_tracks,
    "delete_meeting": lifecycle.delete_meeting,
    "list_profiles": profiles.list_profiles,
    "get_profile": profiles.get_profile,
    "set_profile": profiles.set_profile,
    "delete_profile": profiles.delete_profile,
    "rebuild_catalog": server_info.rebuild_catalog,
}

__all__ = ["HANDLERS", "Handler"]
