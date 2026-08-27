"""``gaia-library`` exporter: propose the minutes to gaia-library via ``propose_update``.

絶対原則 5: writes to gaia-library go through the proposal queue only — this exporter has no
privileged path and nothing lands in the library until a human approves the proposal on the
gaia-library side. The call goes through an injected :class:`narumi.gaia.GaiaClient`, or the
client selected by saved connection settings / environment. With no configured client the
destination is ``engine_unavailable`` (gaia-library is optional).

The proposal is idempotent: ``request_id`` defaults to ``narumi-export-<meeting_id>-v<n>`` so
re-exporting the same minutes version proposes the same update, not a duplicate.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from narumi.bundle import Bundle, utc_now_iso
from narumi.errors import ContractMismatchError, EngineUnavailableError, InvalidArgumentError
from narumi.export.base import ExportOutcome
from narumi.export.common import minutes_markdown_path
from narumi.gaia import ENV_GAIA_URL, GaiaClient

TARGET_TYPE = "interaction"
PATCH_KIND = "meeting"
MIN_REQUEST_ID_CHARS = 8
MAX_REQUEST_ID_BYTES = 256

OPTIONS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "request_id": {
            "type": "string",
            "minLength": MIN_REQUEST_ID_CHARS,
            "maxLength": MAX_REQUEST_ID_BYTES,
            "description": (
                "Idempotency key: at least 8 characters and at most 256 UTF-8 bytes. "
                "Default: narumi-export-<meeting_id>-v<n>."
            ),
        },
    },
    "additionalProperties": False,
}


class GaiaExporter:
    name = "gaia-library"
    description = (
        "議事録を gaia-library への更新提案（propose_update）として送る。提案キュー経由のみで、"
        "承認は gaia-library 側の人間ロールが行う。gaia-library の接続設定が必要"
    )
    options_schema = OPTIONS_SCHEMA

    def __init__(self, client_factory: Callable[[], GaiaClient | None] | None = None) -> None:
        self._client_factory = client_factory

    def export(
        self, bundle: Bundle, *, minutes_version: int, options: dict[str, Any]
    ) -> ExportOutcome:
        request_id = _validate_options(bundle, minutes_version, options)
        client = (self._client_factory or GaiaClient.from_env)()
        if client is None:
            raise EngineUnavailableError(
                f"gaia-library is not configured: configure its connection or set ${ENV_GAIA_URL}",
                details={"env": ENV_GAIA_URL},
            )
        source = minutes_markdown_path(bundle, minutes_version)
        manifest = bundle.manifest
        occurred_at = manifest.recording.started_at or manifest.created_at
        patch: dict[str, Any] = {
            "kind": PATCH_KIND,
            "occurred_at": occurred_at,
            "summary": source.read_text(encoding="utf-8"),
        }
        required_tools = ["propose_update"]
        if manifest.engagement is not None:
            required_tools.append("get_engagement")
        client.require_capabilities(*required_tools)
        if manifest.engagement is not None:
            engagement = client.get_engagement(manifest.engagement, scope=manifest.scope)
            patch["engagement_id"] = engagement["engagement"]["id"]
        provenance = {
            "system": "file",
            "uri": source.resolve().as_uri(),
            "title": f"{manifest.meeting_name} 議事録 v{minutes_version}",
            "note": (
                f"narumi meeting {manifest.meeting_id}; minutes version {minutes_version}; "
                f"meeting occurred at {occurred_at}. Local canonical minutes (Markdown)."
            ),
        }
        result = client.propose_update(
            target_type=TARGET_TYPE,
            action="insert",
            kind="fact",
            patch=patch,
            scope=manifest.scope,
            provenance=provenance,
            request_id=request_id,
        )
        _validate_result(result)
        proposal_id = result["proposal_id"]
        return ExportOutcome(
            destination=self.name,
            ref=f"gaia://proposal/{proposal_id}",
            minutes_version=minutes_version,
            at=utc_now_iso(),
            details={
                "proposal_id": proposal_id,
                "request_id": request_id,
                "status": result["status"],
                "duplicate": result["duplicate"],
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
        request_id = f"narumi-export-{bundle.meeting_id}-v{minutes_version}"
    if not isinstance(request_id, str) or len(request_id.strip()) < MIN_REQUEST_ID_CHARS:
        raise InvalidArgumentError("options.request_id must contain at least 8 characters")
    try:
        request_id_bytes = request_id.encode("utf-8")
    except UnicodeEncodeError:
        raise InvalidArgumentError("options.request_id must be valid UTF-8") from None
    if len(request_id_bytes) > MAX_REQUEST_ID_BYTES:
        raise InvalidArgumentError("options.request_id must be at most 256 UTF-8 bytes")
    return request_id


def _validate_result(result: dict[str, Any]) -> None:
    if (
        not isinstance(result, dict)
        or type(result.get("proposal_id")) is not int
        or result.get("status") not in ("pending", "approved", "rejected")
        or type(result.get("duplicate")) is not bool
    ):
        raise ContractMismatchError(
            "gaia-library returned an invalid propose_update result",
            details={"tool": "propose_update"},
        )
