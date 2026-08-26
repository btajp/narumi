"""Tests for the developer CLI.

Everything that lives in another package (catalog, preprocess, pipeline, engine registries) is
replaced by fake modules injected into ``sys.modules`` so these tests never depend on modules that
may be mid-edit, on ffmpeg (except one opt-in doctor test) or on real engines.
"""

from __future__ import annotations

import dataclasses
import json
import os
import shutil
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from click.testing import CliRunner
from narumi.bundle import Bundle, sha256_file
from narumi.bundle.session import MEETING_ID_RE
from narumi.cli import cli
from narumi.errors import PolicyViolationError


# ---------------------------------------------------------------------------- fakes
@dataclasses.dataclass
class FakeRebuildStats:
    meetings: int
    segments: int
    errors: list[str]


class FakeCatalog:
    instances: list[FakeCatalog] = []
    rows: list[dict[str, Any]] = []

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.upserted: list[str] = []
        self.indexed: list[str] = []
        self.list_calls: list[dict[str, Any]] = []
        self.rebuild_calls: list[Path] = []
        self.closed = False
        FakeCatalog.instances.append(self)

    def upsert_meeting(self, bundle: Bundle) -> None:
        self.upserted.append(bundle.meeting_id)

    def index_segments(self, bundle: Bundle) -> int:
        self.indexed.append(bundle.meeting_id)
        return 0

    def list_meetings(
        self,
        query: str | None = None,
        since: str | None = None,
        until: str | None = None,
        scope: str | list[str] | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        self.list_calls.append(
            {"query": query, "since": since, "until": until, "scope": scope, "limit": limit}
        )
        return list(FakeCatalog.rows)

    def rebuild(self, meetings_root: Path) -> FakeRebuildStats:
        self.rebuild_calls.append(Path(meetings_root))
        return FakeRebuildStats(meetings=2, segments=10, errors=[])

    def get_meeting_row(self, meeting_id: str) -> dict[str, Any] | None:
        return None

    def close(self) -> None:
        self.closed = True


def _module(name: str, **attrs: Any) -> types.ModuleType:
    mod = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(mod, key, value)
    return mod


@pytest.fixture
def fake_catalog(monkeypatch: pytest.MonkeyPatch) -> type[FakeCatalog]:
    FakeCatalog.instances = []
    FakeCatalog.rows = []
    monkeypatch.setitem(
        sys.modules, "narumi.catalog", _module("narumi.catalog", Catalog=FakeCatalog)
    )
    return FakeCatalog


@pytest.fixture
def absent_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "narumi.catalog", None)


DURATIONS = {"mic.wav": 12.5, "system.m4a": 30.0, "screen.mp4": None}


@pytest.fixture
def fake_preprocess(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    probed: list[Path] = []

    def probe_duration(path: Path) -> float | None:
        probed.append(Path(path))
        return DURATIONS.get(Path(path).name)

    monkeypatch.setitem(
        sys.modules,
        "narumi.preprocess",
        _module(
            "narumi.preprocess",
            probe_duration=probe_duration,
            ffmpeg_version=lambda: "fake-ffmpeg",
        ),
    )
    return probed


@pytest.fixture
def absent_preprocess(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "narumi.preprocess", None)


@pytest.fixture
def home(tmp_path: Path) -> Path:
    root = tmp_path / "home"
    root.mkdir()
    return root


@pytest.fixture
def sources(tmp_path: Path) -> dict[str, Path]:
    src = tmp_path / "src"
    src.mkdir()
    files = {
        "mic": src / "mic.wav",
        "system": src / "system.m4a",
        "screen": src / "screen.mp4",
    }
    for i, path in enumerate(files.values()):
        path.write_bytes(bytes([i]) * (1024 * (i + 1)))
    return files


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _run(runner: CliRunner, home: Path, *args: str, env: dict[str, str] | None = None):
    return runner.invoke(cli, ["--data-root", str(home), *args], env=env)


def _error(result) -> dict[str, Any]:
    assert result.exit_code == 2, result.output
    payload = json.loads(result.stderr.strip().splitlines()[-1])
    assert set(payload) == {"error"}
    return payload["error"]


def _import(runner: CliRunner, home: Path, sources: dict[str, Path], *extra: str) -> str:
    result = _run(
        runner,
        home,
        "import-recording",
        "--name",
        "定例",
        "--mic",
        str(sources["mic"]),
        "--system",
        str(sources["system"]),
        *extra,
    )
    assert result.exit_code == 0, result.output
    meeting_id = result.stdout.strip()
    assert MEETING_ID_RE.match(meeting_id)
    return meeting_id


# ---------------------------------------------------------------------------- group
def test_help_lists_commands(runner: CliRunner) -> None:
    result = runner.invoke(cli, ["--help"])
    assert result.exit_code == 0
    for name in (
        "import-recording",
        "process",
        "regenerate",
        "export",
        "show",
        "list",
        "catalog",
        "doctor",
        "config",
    ):
        assert name in result.output
    assert "NARUMI_HOME" in result.output


def test_data_root_from_env(
    runner: CliRunner, tmp_path: Path, fake_catalog, fake_preprocess, sources
):
    env_home = tmp_path / "env-home"
    result = runner.invoke(
        cli,
        ["import-recording", "--name", "x", "--mic", str(sources["mic"])],
        env={"NARUMI_HOME": str(env_home)},
    )
    assert result.exit_code == 0, result.output
    assert (env_home / "meetings" / result.stdout.strip() / "manifest.json").exists()
    assert fake_catalog.instances[0].db_path == env_home / "narumi.db"


# ---------------------------------------------------------------------------- import-recording
def test_import_recording_copy(runner, home, sources, fake_catalog, fake_preprocess) -> None:
    meeting_id = _import(
        runner,
        home,
        sources,
        "--screen",
        str(sources["screen"]),
        "--scope",
        "cloudnative",
        "--engagement",
        "acme",
        "--started-at",
        "2026-08-27T12:05:00+09:00",
    )
    assert meeting_id.startswith("20260827T030500Z-")
    bundle = Bundle.find(home / "meetings", meeting_id)
    m = bundle.manifest
    assert m.status == "recorded"
    assert m.meeting_name == "定例"
    assert m.scope == "cloudnative"
    assert m.engagement == "acme"
    assert set(m.recording.tracks) == {"mic", "system", "screen"}
    for name, src in sources.items():
        rec = m.recording.tracks[name]
        assert rec.path == f"tracks/{name}{src.suffix}"
        dest = bundle.abspath(rec.path)
        assert dest.exists()
        assert rec.sha256 == sha256_file(src)
        assert rec.bytes == src.stat().st_size
        assert rec.duration_sec == DURATIONS[src.name]
        assert not rec.discarded
        assert os.stat(dest).st_ino != os.stat(src).st_ino  # copied, not linked
    assert m.recording.started_at == "2026-08-27T03:05:00Z"
    assert m.recording.duration_sec == 30.0
    assert m.recording.stopped_at == "2026-08-27T03:05:30Z"
    assert m.recording.recorder["importer"] == "narumi-cli"
    assert m.recording.recorder["mode"] == "copy"
    assert sorted(fake_preprocess) == sorted(sources.values())
    # catalog refreshed and closed
    assert len(fake_catalog.instances) == 1
    assert fake_catalog.instances[0].upserted == [meeting_id]
    assert fake_catalog.instances[0].closed
    assert fake_catalog.instances[0].db_path == home / "narumi.db"


def test_import_recording_link(runner, home, sources, fake_catalog, fake_preprocess) -> None:
    meeting_id = _import(runner, home, sources, "--link")
    bundle = Bundle.find(home / "meetings", meeting_id)
    dest = bundle.abspath(bundle.manifest.recording.tracks["mic"].path)
    assert os.stat(dest).st_ino == os.stat(sources["mic"]).st_ino
    assert bundle.manifest.recording.recorder["mode"] == "link"


def test_import_recording_defaults_started_at_to_now(
    runner, home, sources, fake_catalog, fake_preprocess
) -> None:
    meeting_id = _import(runner, home, sources)
    bundle = Bundle.find(home / "meetings", meeting_id)
    started = bundle.manifest.recording.started_at
    assert started is not None and started.endswith("Z")
    assert meeting_id.startswith(started.replace("-", "").replace(":", ""))


def test_import_recording_requires_a_track(runner, home, sources, fake_catalog, fake_preprocess):
    result = _run(
        runner, home, "import-recording", "--name", "x", "--screen", str(sources["screen"])
    )
    err = _error(result)
    assert err["code"] == "invalid_argument"
    assert "--mic" in err["message"]
    assert not any((home / "meetings").glob("*/manifest.json"))


def test_import_recording_rejects_bad_timestamp(
    runner, home, sources, fake_catalog, fake_preprocess
):
    result = _run(
        runner,
        home,
        "import-recording",
        "--name",
        "x",
        "--mic",
        str(sources["mic"]),
        "--started-at",
        "yesterday",
    )
    err = _error(result)
    assert err["code"] == "invalid_argument"
    assert err["details"] == {"value": "yesterday"}
    assert not any((home / "meetings").glob("*/manifest.json"))


def test_import_recording_missing_file_is_usage_error(runner, home, fake_catalog, fake_preprocess):
    result = _run(runner, home, "import-recording", "--name", "x", "--mic", str(home / "nope.wav"))
    assert result.exit_code == 2
    assert "does not exist" in result.output


def test_import_recording_without_catalog_warns(
    runner, home, sources, absent_catalog, fake_preprocess
) -> None:
    result = _run(runner, home, "import-recording", "--name", "y", "--mic", str(sources["mic"]))
    assert result.exit_code == 0, result.output
    assert "warning: catalog unavailable" in result.stderr
    meeting_id = result.stdout.strip()
    assert (home / "meetings" / meeting_id / "manifest.json").exists()


def test_import_recording_without_preprocess_leaves_duration_unknown(
    runner, home, sources, fake_catalog, absent_preprocess
) -> None:
    result = _run(runner, home, "import-recording", "--name", "x", "--mic", str(sources["mic"]))
    assert result.exit_code == 0, result.output
    assert "warning: narumi.preprocess unavailable" in result.stderr
    bundle = Bundle.find(home / "meetings", result.stdout.strip())
    assert bundle.manifest.recording.tracks["mic"].duration_sec is None
    assert bundle.manifest.recording.duration_sec is None
    assert bundle.manifest.recording.stopped_at is None


# ---------------------------------------------------------------------------- show
def test_show(runner, home, sources, fake_catalog, fake_preprocess) -> None:
    meeting_id = _import(runner, home, sources, "--scope", "s")
    result = _run(runner, home, "show", meeting_id)
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["meeting_id"] == meeting_id
    assert payload["meeting_name"] == "定例"
    assert payload["status"] == "recorded"
    assert payload["scope"] == "s"
    assert set(payload["recording"]["tracks"]) == {"mic", "system"}
    assert payload["recording"]["tracks"]["mic"]["path"] == "tracks/mic.wav"
    assert payload["artifacts"] == []
    assert payload["minutes_versions"] == []
    assert payload["latest_minutes_version"] is None
    assert payload["config"]["external_send_policy"] == "local_only"
    assert payload["bundle_path"] == str(home / "meetings" / meeting_id)


def test_show_errors_are_structured(runner, home) -> None:
    err = _error(_run(runner, home, "show", "20260827T000000Z-deadbeef"))
    assert err["code"] == "not_found"
    err = _error(_run(runner, home, "show", "not-an-id"))
    assert err["code"] == "invalid_argument"


# ---------------------------------------------------------------------------- list
def test_list_table_and_scope_shapes(runner, home, fake_catalog) -> None:
    fake_catalog.rows = [
        {
            "meeting_id": "20260827T030500Z-a1b2c3d4",
            "meeting_name": "定例",
            "status": "ready",
            "started_at": "2026-08-27T03:05:00Z",
            "scope": None,
            "latest_minutes_version": 2,
        },
        SimpleNamespace(
            meeting_id="20260827T040500Z-a1b2c3d5",
            meeting_name="other",
            status="recorded",
            started_at="2026-08-27T04:05:00Z",
            scope="cloudnative",
            latest_minutes_version=None,
        ),
    ]
    result = _run(runner, home, "list")
    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    assert lines[0].split() == list(
        ("meeting_id", "status", "started_at", "scope", "latest_minutes_version", "meeting_name")
    )
    assert "20260827T030500Z-a1b2c3d4" in lines[2] and "ready" in lines[2] and "定例" in lines[2]
    assert "20260827T040500Z-a1b2c3d5" in lines[3] and "cloudnative" in lines[3]
    assert fake_catalog.instances[-1].list_calls == [
        {"query": None, "since": None, "until": None, "scope": None, "limit": 50}
    ]
    assert fake_catalog.instances[-1].closed

    _run(runner, home, "list", "--scope", "a", "--query", "budget", "--limit", "5")
    assert fake_catalog.instances[-1].list_calls[-1] == {
        "query": "budget",
        "since": None,
        "until": None,
        "scope": "a",
        "limit": 5,
    }
    _run(runner, home, "list", "--scope", "a", "--scope", "b")
    assert fake_catalog.instances[-1].list_calls[-1]["scope"] == ["a", "b"]


def test_list_empty(runner, home, fake_catalog) -> None:
    result = _run(runner, home, "list")
    assert result.exit_code == 0
    assert result.stdout.strip() == "(no meetings)"


def test_list_without_catalog_is_an_error(runner, home, absent_catalog) -> None:
    err = _error(_run(runner, home, "list"))
    assert err["code"] == "internal"
    assert "catalog unavailable" in err["message"]
    assert err["details"] == {"module": "narumi.catalog"}


# ---------------------------------------------------------------------------- catalog rebuild
def test_catalog_rebuild(runner, home, fake_catalog) -> None:
    result = _run(runner, home, "catalog", "rebuild")
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {"meetings": 2, "segments": 10, "errors": []}
    cat = fake_catalog.instances[-1]
    assert cat.rebuild_calls == [home / "meetings"]
    assert cat.closed


def test_catalog_rebuild_without_catalog(runner, home, absent_catalog) -> None:
    assert _error(_run(runner, home, "catalog", "rebuild"))["code"] == "internal"


# ---------------------------------------------------------------------------- pipeline commands
@pytest.fixture
def fake_pipeline(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[Any]]:
    calls: dict[str, list[Any]] = {"process": [], "regenerate": [], "export": []}

    def process_meeting(bundle: Bundle, *, force: bool = False, progress=None):
        calls["process"].append((bundle.meeting_id, force))
        if progress is not None:
            progress("preprocess", 0.5)
        bundle.manifest.status = "ready"
        bundle.save()
        return SimpleNamespace(
            meeting_id=bundle.meeting_id,
            minutes_version=1,
            stages=["preprocess", "transcribe"],
            skipped=["diarize"],
            unresolved_speakers=["other"],
        )

    def regenerate_meeting(
        bundle: Bundle, *, force=False, progress=None, reason="regenerate", job_id=None
    ):
        calls["regenerate"].append((bundle.meeting_id, force, reason, job_id))
        return SimpleNamespace(
            meeting_id=bundle.meeting_id, minutes_version=2, stages=["align"], skipped=[]
        )

    def export_meeting(
        bundle: Bundle, destination: str, *, options=None, minutes_version=None, request_id=None
    ):
        calls["export"].append((bundle.meeting_id, destination, options, minutes_version))
        return SimpleNamespace(
            destination=destination,
            ref="/tmp/out.md",
            minutes_version=minutes_version or 1,
            at="2026-08-27T03:05:00Z",
            details={"bytes": 12},
        )

    monkeypatch.setitem(
        sys.modules,
        "narumi.pipeline",
        _module(
            "narumi.pipeline",
            process_meeting=process_meeting,
            regenerate_meeting=regenerate_meeting,
            export_meeting=export_meeting,
        ),
    )
    return calls


def test_process(runner, home, sources, fake_catalog, fake_preprocess, fake_pipeline) -> None:
    meeting_id = _import(runner, home, sources)
    result = _run(runner, home, "process", meeting_id, "--force")
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {
        "meeting_id": meeting_id,
        "minutes_version": 1,
        "stages": ["preprocess", "transcribe"],
        "skipped": ["diarize"],
        "unresolved_speakers": ["other"],
    }
    assert "preprocess" in result.stderr  # progress goes to stderr
    assert fake_pipeline["process"] == [(meeting_id, True)]
    # catalog refreshed after processing with the updated bundle
    assert fake_catalog.instances[-1].upserted == [meeting_id]
    assert Bundle.find(home / "meetings", meeting_id).manifest.status == "ready"


def test_regenerate_and_export(
    runner, home, sources, fake_catalog, fake_preprocess, fake_pipeline
) -> None:
    meeting_id = _import(runner, home, sources)
    result = _run(runner, home, "regenerate", meeting_id, "--reason", "vocab fix")
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["minutes_version"] == 2
    assert fake_pipeline["regenerate"] == [(meeting_id, False, "vocab fix", None)]

    result = _run(
        runner, home, "export", meeting_id, "--to", "markdown", "--path", "out.md", "--version", "2"
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {
        "meeting_id": meeting_id,
        "destination": "markdown",
        "ref": "/tmp/out.md",
        "minutes_version": 2,
        "at": "2026-08-27T03:05:00Z",
        "details": {"bytes": 12},
    }
    assert fake_pipeline["export"] == [
        (meeting_id, "markdown", {"output_path": str(Path("out.md").resolve())}, 2)
    ]

    result = _run(runner, home, "export", meeting_id, "--to", "html")
    assert fake_pipeline["export"][-1] == (meeting_id, "html", None, None)


def test_process_failure_still_refreshes_catalog(
    runner, home, sources, fake_catalog, fake_preprocess, monkeypatch
) -> None:
    """The manifest says ``failed`` after a crash; the index must not keep saying ``recorded``."""
    meeting_id = _import(runner, home, sources)

    def failing(bundle: Bundle, *, force: bool = False, progress=None):
        bundle.manifest.status = "failed"
        bundle.save()
        raise PolicyViolationError("nope", details={"provider": "x"})

    monkeypatch.setitem(
        sys.modules, "narumi.pipeline", _module("narumi.pipeline", process_meeting=failing)
    )
    assert _error(_run(runner, home, "process", meeting_id))["code"] == "policy_violation"
    assert fake_catalog.instances[-1].upserted == [meeting_id]
    assert fake_catalog.instances[-1].indexed == []  # merged.json unchanged: not re-indexed
    assert Bundle.find(home / "meetings", meeting_id).manifest.status == "failed"


def test_process_unknown_meeting(runner, home, fake_pipeline) -> None:
    assert _error(_run(runner, home, "process", "20260827T000000Z-deadbeef"))["code"] == "not_found"
    assert fake_pipeline["process"] == []


# ---------------------------------------------------------------------------- config
def test_config_show_and_update(runner, home, sources, fake_catalog, fake_preprocess) -> None:
    meeting_id = _import(runner, home, sources)
    result = _run(runner, home, "config", meeting_id)
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["updated"] == []
    assert payload["config"]["transcription_engine"] == "auto"

    result = _run(
        runner,
        home,
        "config",
        meeting_id,
        "--transcription-engine",
        "fake",
        "--llm-provider",
        "fake",
        "--external-send-policy",
        "subscription_ok",
        "--language",
        "en",
        "--self-name",
        "岡村",
        "--vocab-hint",
        "narumi",
        "--vocab-hint",
        "gaia",
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["updated"] == sorted(
        [
            "transcription_engine",
            "llm_provider",
            "external_send_policy",
            "language",
            "self_name",
            "vocab_hints",
        ]
    )
    cfg = Bundle.find(home / "meetings", meeting_id).manifest.config
    assert cfg.transcription_engine == "fake"
    assert cfg.llm_provider == "fake"
    assert cfg.external_send_policy == "subscription_ok"
    assert cfg.language == "en"
    assert cfg.self_name == "岡村"
    assert cfg.vocab_hints == ["narumi", "gaia"]
    assert cfg.diarization_engine == "none"  # untouched
    assert fake_catalog.instances[-1].upserted == [meeting_id]

    result = _run(runner, home, "config", meeting_id, "--self-name", "")
    assert json.loads(result.stdout)["config"]["self_name"] is None


def test_config_rejects_invalid_policy(runner, home, sources, fake_catalog, fake_preprocess):
    meeting_id = _import(runner, home, sources)
    result = _run(runner, home, "config", meeting_id, "--external-send-policy", "everything")
    assert result.exit_code == 2
    assert "Invalid value" in result.output
    assert Bundle.find(home / "meetings", meeting_id).manifest.config.external_send_policy == (
        "local_only"
    )


# ---------------------------------------------------------------------------- doctor
@pytest.fixture
def fake_registries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(
        sys.modules,
        "narumi.transcribe",
        _module("narumi.transcribe", available_engines=lambda: ["fake", "mlx-whisper"]),
    )
    monkeypatch.setitem(
        sys.modules,
        "narumi.diarize",
        _module("narumi.diarize", available_engines=lambda: [SimpleNamespace(name="none")]),
    )

    def boom() -> list[str]:
        raise RuntimeError("no providers")

    monkeypatch.setitem(sys.modules, "narumi.llm", _module("narumi.llm", available_providers=boom))
    monkeypatch.setitem(sys.modules, "narumi.export", None)


def test_doctor_without_ffmpeg(runner, home, tmp_path, monkeypatch, fake_registries) -> None:
    empty_bin = tmp_path / "bin"
    empty_bin.mkdir()
    monkeypatch.setenv("PATH", str(empty_bin))
    monkeypatch.setenv("NARUMI_RECORDER", str(tmp_path / "missing-recorder"))
    result = _run(runner, home, "doctor")
    assert result.exit_code == 1, result.output
    out = result.stdout
    assert f"data root: {home}" in out
    assert "ffmpeg: missing" in out
    assert "ffprobe: missing" in out
    assert "recorder: missing" in out and "missing-recorder" in out
    assert "transcription engines: fake, mlx-whisper" in out
    assert "diarization engines: none" in out
    assert "llm providers: unavailable: RuntimeError: no providers" in out
    assert "exporters: unavailable:" in out
    assert "ffmpeg/ffprobe are required" in result.stderr


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not on PATH")
def test_doctor_with_ffmpeg(runner, home, tmp_path, monkeypatch, fake_registries) -> None:
    recorder = tmp_path / "narumi-recorder"
    recorder.write_text("#!/bin/sh\nexit 0\n")
    recorder.chmod(0o755)
    monkeypatch.setenv("NARUMI_RECORDER", str(recorder))
    result = _run(runner, home, "doctor")
    assert result.exit_code == 0, result.output
    ffmpeg_line = next(line for line in result.stdout.splitlines() if line.startswith("ffmpeg:"))
    assert "missing" not in ffmpeg_line and "broken" not in ffmpeg_line
    assert ffmpeg_line.split()[1][0].isdigit()
    assert f"recorder: ok ({recorder})" in result.stdout
