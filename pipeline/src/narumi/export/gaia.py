"""``gaia-library`` exporter: propose the minutes to gaia-library via ``propose_update``.

絶対原則 5: writes to gaia-library go through the proposal queue only — this exporter has no
privileged path and nothing lands in the library until a human approves the proposal on the
gaia-library side. The call goes through :class:`narumi.gaia.GaiaClient` (``NARUMI_GAIA_URL``);
without that variable the destination is ``engine_unavailable`` (gaia-library is optional).

The proposal is idempotent: ``request_id`` defaults to ``narumi-export-<meeting_id>-v<n>`` so
re-exporting the same minutes version proposes the same update, not a duplicate.
"""

from __future__ import annotations

from typing import Any

from narumi.bundle import Bundle, utc_now_iso
from narumi.errors import EngineUnavailableError, InvalidArgumentError
from narumi.export.base import ExportOutcome
from narumi.export.common import minutes_markdown_path
from narumi.gaia import ENV_GAIA_URL, GaiaClient

ENTITY_TYPE = "interaction"
PATCH_KIND = "meeting_minutes"

OPTIONS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "request_id": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Idempotency key for the proposal. Default: narumi-export-<meeting_id>-v<n>."
            ),
        },
    },
    "additionalProperties": False,
}


class GaiaExporter:
    name = "gaia-library"
    description = (
        "議事録を gaia-library への更新提案（propose_update）として送る。提案キュー経由のみで、"
        "承認は gaia-library 側の人間ロールが行う。NARUMI_GAIA_URL の設定が必要"
    )
    options_schema = OPTIONS_SCHEMA

    def export(
        self, bundle: Bundle, *, minutes_version: int, options: dict[str, Any]
    ) -> ExportOutcome:
        request_id = _validate_options(bundle, minutes_version, options)
        client = GaiaClient.from_env()
        if client is None:
            raise EngineUnavailableError(
                f"gaia-library is not configured: set ${ENV_GAIA_URL} to its MCP endpoint",
                details={"env": ENV_GAIA_URL},
            )
        source = minutes_markdown_path(bundle, minutes_version)
        manifest = bundle.manifest
        patch = {
            "kind": PATCH_KIND,
            "title": f"{manifest.meeting_name} 議事録 v{minutes_version}",
            "meeting_id": manifest.meeting_id,
            "meeting_name": manifest.meeting_name,
            "engagement": manifest.engagement,
            "minutes_version": minutes_version,
            "content_markdown": source.read_text(encoding="utf-8"),
        }
        result = client.propose_update(
            entity_type=ENTITY_TYPE,
            patch=patch,
            scope=manifest.scope,
            provenance=f"minutes://meeting/{manifest.meeting_id}",
            request_id=request_id,
        )
        proposal_id = str(result.get("proposal_id") or result.get("id") or "")
        ref = str(result.get("ref") or f"gaia://proposal/{proposal_id or request_id}")
        return ExportOutcome(
            destination=self.name,
            ref=ref,
            minutes_version=minutes_version,
            at=utc_now_iso(),
            details={
                "proposal_id": proposal_id or None,
                "request_id": request_id,
                "status": result.get("status"),
            },
        )


def _validate_options(bundle: Bundle, minutes_version: int, options: dict[str, Any]) -> str:
    unknown = sorted(set(options) - set(OPTIONS_SCHEMA["properties"]))
    if unknown:
        raise InvalidArgumentError(
            f"unknown export options: {', '.join(unknown)}", details={"unknown": unknown}
        )
    request_id = options.get("request_id")
    if request_id is None:
        return f"narumi-export-{bundle.meeting_id}-v{minutes_version}"
    if not isinstance(request_id, str) or not request_id.strip():
        raise InvalidArgumentError("options.request_id must be a non-empty string")
    return request_id.strip()
