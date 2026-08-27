"""Gaia export: actual proposal contract, scoped engagement lookup, and stable provenance."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from urllib.parse import unquote, urlparse

import pytest
from narumi.bundle import Bundle, MinutesVersionRecord
from narumi.errors import (
    ContractMismatchError,
    EngineUnavailableError,
    InvalidArgumentError,
    NotFoundError,
)
from narumi.export.gaia import GaiaExporter
from narumi.export.registry import get_exporter
from narumi.gaia import ENV_GAIA_URL, GaiaClient

MINUTES = "# 定例 議事録\n\n## 決定事項\n\n- リリースは 9/10\n\n![資料](slides/slide-1.png)\n"


class StubGaia:
    def __init__(self) -> None:
        self.url = "http://127.0.0.1:8877/mcp"
        self.calls: list[tuple[str, dict]] = []
        self.result = {"proposal_id": 17, "status": "pending", "duplicate": False}
        self.previous: dict | None = None
        self.engagement_error: Exception | None = None

    def require_capabilities(self, *tools):
        self.calls.append(("require_capabilities", {"tools": tools}))

    def get_engagement(self, name, *, scope=None):
        self.calls.append(("get_engagement", {"name": name, "scope": scope}))
        if self.engagement_error is not None:
            raise self.engagement_error
        return {
            "engagement": {"id": 42, "name": name, "scope": scope or "client-a"},
            "people": [],
            "facts": [],
            "refs": [],
            "glossary": [],
            "interactions": [],
        }

    def propose_update(self, **arguments):
        self.calls.append(("propose_update", deepcopy(arguments)))
        duplicate = arguments == self.previous
        self.previous = deepcopy(arguments)
        result = deepcopy(self.result)
        if "duplicate" in result and type(result["duplicate"]) is bool:
            result["duplicate"] = duplicate
        return result


def bundle_with_minutes(tmp_path: Path) -> Bundle:
    bundle = Bundle.create(
        tmp_path / "meetings",
        meeting_name="定例ミーティング",
        engagement="acme",
        scope="client-a",
    )
    bundle.manifest.recording.started_at = "2026-08-27T03:00:00Z"
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


def exporter_for(client: StubGaia) -> GaiaExporter:
    return GaiaExporter(client_factory=lambda: client)  # type: ignore[return-value]


def proposal(client: StubGaia) -> dict:
    return next(arguments for tool, arguments in reversed(client.calls) if tool == "propose_update")


def test_export_uses_only_public_reads_and_proposal_write(tmp_path: Path):
    client = StubGaia()
    bundle = bundle_with_minutes(tmp_path)
    outcome = exporter_for(client).export(bundle, minutes_version=1, options={})

    assert outcome.destination == "gaia-library" and outcome.minutes_version == 1
    assert outcome.ref == "gaia://proposal/17"
    assert outcome.details == {
        "proposal_id": 17,
        "status": "pending",
        "duplicate": False,
        "request_id": f"narumi-export-{bundle.meeting_id}-v1",
    }
    assert [name for name, _ in client.calls] == [
        "require_capabilities",
        "get_engagement",
        "propose_update",
    ]
    assert client.calls[1] == ("get_engagement", {"name": "acme", "scope": "client-a"})
    args = proposal(client)
    assert set(args) == {
        "target_type",
        "action",
        "kind",
        "patch",
        "scope",
        "provenance",
        "request_id",
    }
    assert args["target_type"] == "interaction" and args["action"] == "insert"
    assert args["kind"] == "fact" and args["scope"] == "client-a"
    assert args["patch"] == {
        "kind": "meeting",
        "occurred_at": "2026-08-27T03:00:00Z",
        "summary": MINUTES,
        "engagement_id": 42,
    }
    provenance = args["provenance"]
    assert provenance["system"] == "file"
    source = Path(unquote(urlparse(provenance["uri"]).path))
    assert source == bundle.abspath("minutes/v1/minutes.md").resolve()
    assert source.read_text(encoding="utf-8") == MINUTES
    assert provenance["title"] == "定例ミーティング 議事録 v1"
    assert bundle.meeting_id in provenance["note"]
    assert "2026-08-27T03:00:00Z" in provenance["note"]


def test_repeat_export_payload_and_request_id_do_not_depend_on_export_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    client = StubGaia()
    bundle = bundle_with_minutes(tmp_path)
    exporter = exporter_for(client)
    monkeypatch.setattr("narumi.export.gaia.utc_now_iso", lambda: "2026-08-27T05:00:00Z")
    first = exporter.export(bundle, minutes_version=1, options={})
    first_arguments = proposal(client)
    monkeypatch.setattr("narumi.export.gaia.utc_now_iso", lambda: "2026-08-28T05:00:00Z")
    second = exporter.export(bundle, minutes_version=1, options={})
    assert proposal(client) == first_arguments
    assert first.ref == second.ref and first.at != second.at
    assert not first.details["duplicate"] and second.details["duplicate"]


def test_no_recording_start_uses_stable_bundle_creation_time(tmp_path: Path):
    client = StubGaia()
    bundle = bundle_with_minutes(tmp_path)
    bundle.manifest.recording.started_at = None
    exporter_for(client).export(bundle, minutes_version=1, options={})
    assert proposal(client)["patch"]["occurred_at"] == bundle.manifest.created_at


def test_no_engagement_uses_only_proposal_capability(tmp_path: Path):
    client = StubGaia()
    bundle = bundle_with_minutes(tmp_path)
    bundle.manifest.engagement = None
    bundle.manifest.scope = None
    exporter_for(client).export(bundle, minutes_version=1, options={})
    assert client.calls[0] == ("require_capabilities", {"tools": ("propose_update",)})
    assert [name for name, _ in client.calls] == ["require_capabilities", "propose_update"]
    assert "engagement_id" not in proposal(client)["patch"]
    assert proposal(client)["scope"] is None


def test_unknown_engagement_does_not_create_an_unrelated_proposal(tmp_path: Path):
    client = StubGaia()
    client.engagement_error = NotFoundError("engagement not found in scope")
    with pytest.raises(NotFoundError):
        exporter_for(client).export(bundle_with_minutes(tmp_path), minutes_version=1, options={})
    assert not any(name == "propose_update" for name, _ in client.calls)


@pytest.mark.parametrize("request_id", ["custom-request-42", "x" * 256, "あ" * 85])
def test_explicit_request_id_is_used_exactly(tmp_path: Path, request_id: str):
    client = StubGaia()
    outcome = exporter_for(client).export(
        bundle_with_minutes(tmp_path), minutes_version=1, options={"request_id": request_id}
    )
    assert outcome.details["request_id"] == request_id
    assert proposal(client)["request_id"] == request_id


def test_missing_configuration_is_engine_unavailable(tmp_path: Path):
    with pytest.raises(EngineUnavailableError) as excinfo:
        GaiaExporter(client_factory=lambda: None).export(
            bundle_with_minutes(tmp_path), minutes_version=1, options={}
        )
    assert ENV_GAIA_URL in str(excinfo.value)


@pytest.mark.parametrize(
    "options",
    [
        {"destination_id": "x"},
        {"data_root": "/tmp"},
        {"api_key": "secret"},
        {"request_id": " "},
        {"request_id": "short"},
        {"request_id": "x" * 257},
        {"request_id": "あ" * 86},
        {"request_id": 42},
        {"request_id": True},
        {"request_id": "bad-utf8-\ud800"},
    ],
)
def test_invalid_options_are_rejected_before_obtaining_a_client(tmp_path: Path, options: dict):
    def unexpected_client():
        pytest.fail("invalid options must not initialize a Gaia connection")

    with pytest.raises(InvalidArgumentError):
        GaiaExporter(client_factory=unexpected_client).export(
            bundle_with_minutes(tmp_path), minutes_version=1, options=options
        )


@pytest.mark.parametrize(
    "result",
    [
        {},
        {"id": 17, "status": "pending", "duplicate": False},
        {"proposal_id": "17", "status": "pending", "duplicate": False},
        {"proposal_id": True, "status": "pending", "duplicate": False},
        {"proposal_id": 17, "status": "queued", "duplicate": False},
        {"proposal_id": 17, "status": "pending"},
        {"proposal_id": 17, "status": "pending", "duplicate": "false"},
    ],
)
def test_malformed_proposal_result_never_fabricates_success(tmp_path: Path, result: dict):
    client = StubGaia()
    client.result = result
    with pytest.raises(ContractMismatchError):
        exporter_for(client).export(bundle_with_minutes(tmp_path), minutes_version=1, options={})


def test_loopback_gaia_export_works_with_local_only(tmp_path: Path):
    bundle = bundle_with_minutes(tmp_path)
    assert bundle.manifest.config.external_send_policy == "local_only"
    outcome = exporter_for(StubGaia()).export(bundle, minutes_version=1, options={})
    assert outcome.ref == "gaia://proposal/17"


def test_default_exporter_uses_configured_client_factory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    client = StubGaia()
    monkeypatch.setattr(GaiaClient, "from_env", lambda: client)
    get_exporter("gaia-library").export(
        bundle_with_minutes(tmp_path), minutes_version=1, options={}
    )
    assert proposal(client)["scope"] == "client-a"


def test_gaia_exporter_is_registered():
    assert isinstance(get_exporter("gaia-library"), GaiaExporter)
