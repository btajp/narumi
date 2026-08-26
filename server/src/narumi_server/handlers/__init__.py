"""Tool name → handler registry. Every contract tool must have exactly one entry here."""

from __future__ import annotations

from narumi_server.handlers import contexts, meetings, processing, recording, server_info
from narumi_server.handlers.common import Handler

HANDLERS: dict[str, Handler] = {
    "get_server_info": server_info.get_server_info,
    "start_recording": recording.start_recording,
    "stop_recording": recording.stop_recording,
    "list_meetings": meetings.list_meetings,
    "get_meeting": meetings.get_meeting,
    "get_transcript": meetings.get_transcript,
    "register_context": contexts.register_context,
    "regenerate": processing.regenerate,
    "set_meeting_config": meetings.set_meeting_config,
    "export_minutes": processing.export_minutes,
    "list_export_destinations": server_info.list_export_destinations,
    "get_job_status": processing.get_job_status,
}

__all__ = ["HANDLERS", "Handler"]
