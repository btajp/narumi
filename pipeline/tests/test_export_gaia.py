"""gaia-library exporter: propose_update through the fake gaia MCP server (絶対原則 5)."""

from __future__ import annotations

from pathlib import Path

import pytest
from narumi.bundle import Bundle, MinutesVersionRecord
from narumi.errors import EngineUnavailableError, InvalidArgumentError
from narumi.export.gaia import GaiaExporter
from narumi.export.registry import get_exporter
from narumi.gaia import ENV_GAIA_URL

from .test_gaia_client import FakeGaiaServer, tool_ok

MINUTES = "# 定例 議事録\n\n## 決定事項\n\n- リリースは 9/10\n"


@pytest.fixture()
def gaia_server():
    server = FakeGaiaServer()
    server.tools["propose_update"] = lambda args: tool_ok(
        {"proposal_id": "prop-1", "status": "queued", "ref": "gaia://proposal/prop-1"}
    )
    server.start()
    try:
        yield server
    finally:
        server.stop()


def bundle_with_minutes(tmp_path: Path) -> Bundle:
    bundle = Bundle.create(
        tmp_path / "meetings",
        meeting_name="定例ミーティング",
        engagement="acme",
        scope="client-a",
    )
    v1 = bundle.minutes_dir(1)
    (v1 / "minutes.md").write_text(MINUTES, encoding="utf-8")
    bundle.manifest.minutes_versions.append(
        MinutesVersionRecord(
            version=1,
            path="minutes/v1/minutes.md",
            generated_at="2026-08-27T03:10:00Z",
            provider="none",
        )
    )
    bundle.save()
    return bundle


def test_export_goes_through_propose_update_only(
    tmp_path: Path, gaia_server: FakeGaiaServer, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv(ENV_GAIA_URL, gaia_server.url)
    bundle = bundle_with_minutes(tmp_path)
    outcome = get_exporter("gaia-library").export(bundle, minutes_version=1, options={})

    assert outcome.destination == "gaia-library" and outcome.minutes_version == 1
    assert outcome.ref == "gaia://proposal/prop-1"
    assert outcome.details["proposal_id"] == "prop-1"
    assert outcome.details["status"] == "queued"

    calls = gaia_server.call_frames()
    assert [c["params"]["name"] for c in calls] == ["propose_update"]  # no other write path
    args = calls[0]["params"]["arguments"]
    assert args["entity_type"] == "interaction"
    assert args["scope"] == "client-a"
    assert args["provenance"] == f"minutes://meeting/{bundle.meeting_id}"
    assert args["request_id"] == f"narumi-export-{bundle.meeting_id}-v1"
    patch = args["patch"]
    assert patch["kind"] == "meeting_minutes"
    assert patch["meeting_id"] == bundle.meeting_id
    assert patch["meeting_name"] == "定例ミーティング"
    assert patch["engagement"] == "acme"
    assert patch["minutes_version"] == 1
    assert patch["content_markdown"] == MINUTES


def test_explicit_request_id_is_used(
    tmp_path: Path, gaia_server: FakeGaiaServer, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv(ENV_GAIA_URL, gaia_server.url)
    bundle = bundle_with_minutes(tmp_path)
    outcome = get_exporter("gaia-library").export(
        bundle, minutes_version=1, options={"request_id": "req-42"}
    )
    assert outcome.details["request_id"] == "req-42"
    args = gaia_server.call_frames()[0]["params"]["arguments"]
    assert args["request_id"] == "req-42"


def test_missing_gaia_url_is_engine_unavailable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(ENV_GAIA_URL, raising=False)
    bundle = bundle_with_minutes(tmp_path)
    with pytest.raises(EngineUnavailableError) as excinfo:
        get_exporter("gaia-library").export(bundle, minutes_version=1, options={})
    assert ENV_GAIA_URL in str(excinfo.value)


def test_unknown_options_are_rejected_before_any_call(
    tmp_path: Path, gaia_server: FakeGaiaServer, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv(ENV_GAIA_URL, gaia_server.url)
    bundle = bundle_with_minutes(tmp_path)
    with pytest.raises(InvalidArgumentError):
        get_exporter("gaia-library").export(
            bundle, minutes_version=1, options={"destination_id": "x"}
        )
    with pytest.raises(InvalidArgumentError):
        get_exporter("gaia-library").export(bundle, minutes_version=1, options={"request_id": " "})
    assert gaia_server.call_frames() == []


def test_gaia_exporter_is_registered():
    assert isinstance(get_exporter("gaia-library"), GaiaExporter)
