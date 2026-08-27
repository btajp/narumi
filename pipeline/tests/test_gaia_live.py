"""Opt-in integration with a real Gaia binary and an entirely temporary config/database.

Set NARUMI_GAIA_BIN to an executable Gaia build with HTTP support to enable these tests.
Without it they skip, including in the normal repository-wide suite. No existing Gaia
server, user credentials, home directory, or external provider is consulted.
"""

from __future__ import annotations

import hashlib
import json
import os
import selectors
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from narumi.brief import build_brief, run_brief
from narumi.bundle import Bundle, MinutesVersionRecord
from narumi.errors import ErrorCode, NarumiError
from narumi.export.gaia import GaiaExporter
from narumi.gaia import GaiaClient

HUMAN = "narumi-live-human"
AGENT = "narumi-live-agent"
DEFAULT_SCOPE = "narumi-live-default"
MEETING_SCOPE = "narumi-live-meeting"
ENGAGEMENT_NAME = "NarumiE2EMeeting"
TERM = "GaiaScopeTerm"
DECOY = "DO_NOT_INCLUDE_DEFAULT_SCOPE"
FACT = "会議 scope の決定事項を確認済み"
PREVIOUS = "前回はローカル会議録の連携を確認した"
SNAPSHOT = "登録時点の会議資料の要点"
MINUTES = "# Narumi E2E 議事録\n\nローカルから提案キューまでの実接続を確認した。\n"


@dataclass
class LiveGaia:
    binary: Path
    root: Path
    url: str = ""
    engagement_id: int = 0
    default_engagement_id: int = 0
    person_id: int = 0
    reference_uri: str = ""
    _keys: dict[str, str] = field(default_factory=dict, repr=False)
    _sequence: int = field(default=0, repr=False)

    @property
    def environment(self) -> dict[str, str]:
        # An allowlist, not os.environ.copy(): explicit paths prevent any home/config lookup.
        return {
            "PATH": os.environ.get("PATH", os.defpath),
            "GAIA_CONFIG": str(self.root / "config.toml"),
            "GAIA_DB": str(self.root / "gaia.db"),
            "RUST_LOG": "off",
            "RUST_BACKTRACE": "0",
            "NO_COLOR": "1",
        }

    def command(self, *args: str, actor: str | None = HUMAN) -> list[str]:
        command = [str(self.binary), "--config", self.environment["GAIA_CONFIG"], "--json"]
        if actor is not None:
            command.extend(("--client", actor))
        return [*command, *args]

    def cli(self, *args: str, empty_output: bool = False) -> dict[str, Any]:
        try:
            completed = subprocess.run(
                self.command(*args),
                cwd=self.root,
                env=self.environment,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            pytest.fail("Gaia CLI test setup could not complete; output withheld", pytrace=False)
        if completed.returncode:
            pytest.fail(
                f"Gaia CLI test command {args[0]} failed (exit {completed.returncode}); "
                "stdout/stderr withheld to protect issued keys",
                pytrace=False,
            )
        if empty_output:
            return {}
        try:
            payload = json.loads(completed.stdout)
        except (ValueError, UnicodeError):
            pytest.fail("Gaia CLI did not return JSON; output withheld", pytrace=False)
        if not isinstance(payload, dict):
            pytest.fail(
                "Gaia CLI returned an unexpected JSON shape; output withheld", pytrace=False
            )
        return payload

    def remember_key(self, actor: str, output: dict[str, Any]) -> None:
        key = output.get("key")
        if not isinstance(key, str) or not key.startswith("gaia_"):
            pytest.fail("Gaia CLI did not issue a valid test key; output withheld", pytrace=False)
        self._keys[actor] = key

    def client(self, actor: str = AGENT) -> GaiaClient:
        return GaiaClient(self.url, api_key=self._keys[actor], timeout=5)

    def seed(self, target: str, patch: dict[str, Any], *, scope: str = MEETING_SCOPE) -> int:
        """Human-only fixture seeding through Gaia's public proposal and approval commands."""
        self._sequence += 1
        proposed = self.cli(
            "propose",
            target,
            "insert",
            "--scope",
            scope,
            "--patch",
            json.dumps(patch, ensure_ascii=False),
            "--request-id",
            f"narumi-live-seed-{self._sequence:04d}",
        )
        approved = self.cli("approve", str(proposed["proposal_id"]), "--scope", scope)
        assert approved["status"] == "approved"
        assert approved["result"]["target_type"] == target
        return approved["result"]["id"]

    def assert_no_credentials(self, text: str) -> None:
        for key in self._keys.values():
            if key in text or hashlib.sha256(key.encode()).hexdigest() in text:
                pytest.fail("Gaia credential material appeared in a public artifact", pytrace=False)


def _seed(live: LiveGaia) -> None:
    live.cli("init", "--affiliation", DEFAULT_SCOPE, empty_output=True)
    live.cli("affiliation", "add", MEETING_SCOPE)
    live.remember_key(HUMAN, live.cli("client", "keygen", HUMAN))
    live.remember_key(
        AGENT,
        live.cli(
            "client",
            "add",
            AGENT,
            "--role",
            "agent",
            "--default-scope",
            DEFAULT_SCOPE,
            "--generate-key",
        ),
    )
    live.default_engagement_id = live.seed(
        "engagement", {"name": ENGAGEMENT_NAME}, scope=DEFAULT_SCOPE
    )
    live.seed(
        "glossary",
        {"term": DECOY, "engagement_id": live.default_engagement_id},
        scope=DEFAULT_SCOPE,
    )
    live.seed(
        "fact",
        {
            "entity_type": "engagement",
            "entity_id": live.default_engagement_id,
            "statement": DECOY,
        },
        scope=DEFAULT_SCOPE,
    )
    live.person_id = live.seed(
        "person", {"name": "田中 太郎", "aliases": [{"alias": "tanaka-e2e"}]}
    )
    live.engagement_id = live.seed(
        "engagement",
        {"name": ENGAGEMENT_NAME, "status": "active", "people": [{"person_id": live.person_id}]},
    )
    live.seed(
        "glossary",
        {
            "term": TERM,
            "reading": "ガイアスコープ用語",
            "definition": "会議専用の用語",
            "engagement_id": live.engagement_id,
        },
    )
    live.seed(
        "fact", {"entity_type": "engagement", "entity_id": live.engagement_id, "statement": FACT}
    )
    reference = live.root / "source.md"
    reference.write_text("# 前回の会議資料\n", encoding="utf-8")
    live.reference_uri = reference.as_uri()
    live.seed(
        "ref",
        {
            "target_type": "engagement",
            "target_id": live.engagement_id,
            "system": "file",
            "uri": live.reference_uri,
            "title": "前回資料",
            "note": "前回会議の確認済み資料",
            "snapshot": SNAPSHOT,
        },
    )
    live.seed(
        "interaction",
        {
            "kind": "meeting",
            "occurred_at": "2026-08-27T00:00:00Z",
            "summary": PREVIOUS,
            "engagement_id": live.engagement_id,
            "person_ids": [live.person_id],
        },
    )


def _ready_url(process: subprocess.Popen[bytes]) -> str:
    assert process.stdout is not None
    buffer = b""
    deadline = time.monotonic() + 15
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ)
        while time.monotonic() < deadline:
            if process.poll() is not None:
                pytest.fail(
                    "Gaia HTTP process exited before readiness; output withheld", pytrace=False
                )
            for key, _ in selector.select(timeout=0.1):
                chunk = os.read(key.fd, 4096)
                if not chunk:
                    continue
                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    try:
                        record = json.loads(line)
                    except (ValueError, UnicodeError):
                        continue
                    if isinstance(record, dict) and record.get("status") == "listening":
                        url = record.get("url")
                        if isinstance(url, str):
                            # The production constructor independently rejects remote endpoints.
                            return GaiaClient(url, timeout=5).url
    pytest.fail("Gaia HTTP readiness timed out; stdout/stderr withheld", pytrace=False)


def _stop(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        process.send_signal(signal.SIGINT)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


@pytest.fixture(scope="module")
def live_gaia(tmp_path_factory: pytest.TempPathFactory):
    configured = os.environ.get("NARUMI_GAIA_BIN")
    if not configured:
        pytest.skip("set NARUMI_GAIA_BIN to opt into the isolated real-Gaia test")
    binary = Path(configured).expanduser().resolve()
    if not binary.is_file() or not os.access(binary, os.X_OK):
        pytest.fail("NARUMI_GAIA_BIN must identify an executable Gaia binary", pytrace=False)
    live = LiveGaia(binary=binary, root=tmp_path_factory.mktemp("gaia-live"))
    _seed(live)
    try:
        process = subprocess.Popen(
            live.command("serve", "--http", "--port", "0", actor=None),
            cwd=live.root,
            env=live.environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        pytest.fail("Gaia HTTP process could not start; output withheld", pytrace=False)
    drainer = None
    try:
        live.url = _ready_url(process)

        def drain() -> None:
            assert process.stdout is not None
            for _ in process.stdout:
                pass

        drainer = threading.Thread(target=drain, daemon=True)
        drainer.start()
        yield live
    finally:
        _stop(process)
        if drainer is not None:
            drainer.join(timeout=2)
        if process.stdout is not None:
            process.stdout.close()


def _bundle(root: Path) -> Bundle:
    bundle = Bundle.create(
        root / "meetings",
        meeting_name=ENGAGEMENT_NAME,
        engagement=ENGAGEMENT_NAME,
        scope=MEETING_SCOPE,
    )
    bundle.manifest.recording.started_at = "2026-08-27T03:00:00Z"
    minutes = bundle.minutes_dir(1) / "minutes.md"
    minutes.write_text(MINUTES, encoding="utf-8")
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


def test_live_metadata_speakers_and_scoped_brief(live_gaia: LiveGaia, tmp_path: Path):
    client = live_gaia.client()
    info = client.require_capabilities(
        "get_engagement", "get_glossary", "search_context", "resolve_speakers", "propose_update"
    )
    assert info["name"] == "gaia_library" and info["contract_version"].startswith("1.")
    assert "http" in info["protocol"]["transports"]
    assert info["client"] == {"name": AGENT, "role": "agent", "default_scope": DEFAULT_SCOPE}
    assert "approve_proposal" not in info["capabilities"]["tools"]
    assert live_gaia.client(HUMAN).get_server_info()["client"]["role"] == "human"
    assert (
        client.get_engagement(ENGAGEMENT_NAME)["engagement"]["id"]
        == live_gaia.default_engagement_id
    )
    glossary = client.get_glossary(ENGAGEMENT_NAME, scope=MEETING_SCOPE)
    assert TERM in glossary["vocabulary_hints"] and DECOY not in glossary["vocabulary_hints"]
    speakers = client.resolve_speakers(
        ["tanaka-e2e"], engagement=ENGAGEMENT_NAME, scope=MEETING_SCOPE
    )
    assert speakers["results"][0]["status"] == "matched"
    assert speakers["results"][0]["person"]["id"] == live_gaia.person_id

    bundle = _bundle(tmp_path)
    brief = build_brief(bundle, client)
    assert TERM in brief.vocab_hints and "tanaka-e2e" in brief.vocab_hints
    assert any(person.person_id == live_gaia.person_id for person in brief.participants)
    assert FACT in brief.background and PREVIOUS in brief.previous_points
    assert SNAPSHOT in "\n".join(brief.background)
    assert any(source.uri == live_gaia.reference_uri for source in brief.sources)
    assert brief.gaia_context["search_context"]["scopes"] == [MEETING_SCOPE]
    assert brief.gaia_context["get_engagement"]["engagement"]["id"] == live_gaia.engagement_id
    assert DECOY not in brief.model_dump_json()
    assert run_brief(bundle, client).skipped
    for path in bundle.path.rglob("*.json"):
        live_gaia.assert_no_credentials(path.read_text(encoding="utf-8"))


def test_live_implicit_scope_uses_authenticated_default(live_gaia: LiveGaia, tmp_path: Path):
    bundle = Bundle.create(
        tmp_path / "meetings", meeting_name=ENGAGEMENT_NAME, engagement=ENGAGEMENT_NAME
    )
    brief = build_brief(bundle, live_gaia.client())
    detail = brief.gaia_context["get_engagement"]["engagement"]
    assert detail["id"] == live_gaia.default_engagement_id and detail["scope"] == DEFAULT_SCOPE
    assert brief.gaia_context["search_context"]["scopes"] == [DEFAULT_SCOPE]
    assert DECOY in brief.vocab_hints and TERM not in brief.vocab_hints
    assert FACT not in brief.background
    for path in bundle.path.rglob("*.json"):
        live_gaia.assert_no_credentials(path.read_text(encoding="utf-8"))


def test_live_export_is_pending_idempotent_and_cannot_be_approved_by_agent(live_gaia, tmp_path):
    client = live_gaia.client()
    bundle = _bundle(tmp_path)
    before = client.get_engagement(ENGAGEMENT_NAME, scope=MEETING_SCOPE)["interactions"]
    exporter = GaiaExporter(client_factory=lambda: client)
    first = exporter.export(bundle, minutes_version=1, options={})
    second = exporter.export(bundle, minutes_version=1, options={})
    proposal_id = first.details["proposal_id"]
    assert first.details["status"] == "pending" and first.details["duplicate"] is False
    assert second.details["proposal_id"] == proposal_id and second.details["duplicate"] is True
    pending = client.call("list_proposals", {"scope": MEETING_SCOPE, "status": "pending"})
    proposals = [item for item in pending["proposals"] if item["id"] == proposal_id]
    assert len(proposals) == 1
    proposal = proposals[0]
    assert proposal["target_type"] == "interaction" and proposal["action"] == "insert"
    assert proposal["kind"] == "fact" and proposal["scope"] == MEETING_SCOPE
    assert proposal["proposed_by"] == AGENT
    assert proposal["request_id"] == f"narumi-export-{bundle.meeting_id}-v1"
    assert proposal["patch"] == {
        "kind": "meeting",
        "occurred_at": bundle.manifest.recording.started_at,
        "summary": MINUTES,
        "engagement_id": live_gaia.engagement_id,
    }
    assert proposal["provenance"]["system"] == "file"
    assert (
        proposal["provenance"]["uri"] == (bundle.minutes_dir(1) / "minutes.md").resolve().as_uri()
    )
    assert proposal["provenance"]["note"]
    with pytest.raises(NarumiError) as denied:
        client.call("approve_proposal", {"proposal_id": proposal_id, "scope": MEETING_SCOPE})
    assert denied.value.code == ErrorCode.SCOPE_DENIED
    assert denied.value.details["gaia_code"] == "unauthorized"
    assert client.get_engagement(ENGAGEMENT_NAME, scope=MEETING_SCOPE)["interactions"] == before
    still_pending = client.call("list_proposals", {"scope": MEETING_SCOPE, "status": "pending"})
    assert any(item["id"] == proposal_id for item in still_pending["proposals"])
    live_gaia.assert_no_credentials(json.dumps(proposal, ensure_ascii=False))


@pytest.mark.parametrize("api_key", [None, "gaia_invalid_00000000000000000000000000000000"])
def test_live_invalid_or_missing_bearer_is_rejected(live_gaia: LiveGaia, api_key):
    with pytest.raises(NarumiError) as denied:
        GaiaClient(live_gaia.url, api_key=api_key, timeout=5).get_server_info()
    assert denied.value.code == ErrorCode.SCOPE_DENIED
    assert denied.value.details["status"] == 401
    live_gaia.assert_no_credentials(json.dumps(denied.value.to_payload(), ensure_ascii=False))
