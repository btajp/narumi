"""History and GitHub verification gates with in-memory responses, not real commands."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def verifier(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    spec = importlib.util.spec_from_file_location(
        "narumi_release_verify", ROOT / "scripts/release_verify.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def no_process(*args, **kwargs):
        raise AssertionError("Real commands are forbidden in release verifier unit tests")

    monkeypatch.setattr(module.subprocess, "run", no_process)
    return module


def old_feed(version="0.1.0", build="24"):
    return (
        '<rss xmlns:sparkle="http://www.andymatuschak.org/xml-namespaces/sparkle">'
        f"<channel><item><sparkle:version>{build}</sparkle:version>"
        f"<sparkle:shortVersionString>{version}</sparkle:shortVersionString>"
        "<enclosure /></item></channel></rss>"
    ).encode()


def history(monkeypatch, verifier, *, tag="v0.1.0", feed=None, draft=False, missing=False):
    content = old_feed() if feed is None else feed
    assets = [] if missing else [{"name": "appcast.xml", "size": len(content), "id": 10}]
    release = {"tag_name": tag, "draft": draft, "prerelease": False, "assets": assets}
    calls = []

    def fake_run(*command, **kwargs):
        calls.append(command)
        if command[2].endswith("/releases"):
            return json.dumps([[release], []]).encode()
        assert command[2].endswith("/assets/10")
        return content

    monkeypatch.setattr(verifier, "run", fake_run)
    return calls


def test_previous_release_monotonicity(verifier, monkeypatch):
    calls = history(monkeypatch, verifier)
    verifier.check_history("0.1.1", 25)
    assert len(calls) == 2
    assert "--paginate" in calls[0] and "--slurp" in calls[0]


@pytest.mark.parametrize(
    "version,build", [("0.1.0", 25), ("0.0.9", 25), ("0.1.1", 24), ("0.1.1", 23)]
)
def test_non_increasing_version_or_build_rejected(verifier, monkeypatch, version, build):
    history(monkeypatch, verifier)
    with pytest.raises(verifier.ReleaseError):
        verifier.check_history(version, build)


@pytest.mark.parametrize("feed", [old_feed("0.0.9"), old_feed(build="024"), b"not XML"])
def test_previous_feed_must_match_release(verifier, monkeypatch, feed):
    history(monkeypatch, verifier, feed=feed)
    with pytest.raises(verifier.ReleaseError):
        verifier.check_history("0.1.1", 25)


def test_missing_historical_feed_fails_closed(verifier, monkeypatch):
    history(monkeypatch, verifier, missing=True)
    with pytest.raises(verifier.ReleaseError):
        verifier.check_history("0.1.1", 25)


def test_existing_draft_rejected_except_during_verification(verifier, monkeypatch):
    calls = history(monkeypatch, verifier, tag="v0.1.1", draft=True)
    with pytest.raises(verifier.ReleaseError):
        verifier.check_history("0.1.1", 25)
    verifier.check_history("0.1.1", 25, allow_current=True)
    assert len(calls) == 2


def test_command_failure_is_redacted(verifier, monkeypatch):
    def fail(*args, **kwargs):
        raise verifier.subprocess.CalledProcessError(1, ["command", "secret"], stderr=b"secret")

    monkeypatch.setattr(verifier.subprocess, "run", fail)
    with pytest.raises(verifier.ReleaseError) as caught:
        verifier.run("gh", "secret")
    assert "secret" not in str(caught.value)


@pytest.fixture
def remote(verifier, monkeypatch, tmp_path):
    payloads = {"narumi-0.1.1.zip": b"fake-zip-content", "appcast.xml": b"fake-appcast-content"}
    sealed = {
        "version": "0.1.1",
        "build": 25,
        "commit": "1" * 40,
        "public_key": "fixture",
        "signature": "fixture",
        "info": {},
        "assets": {
            name: {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
            for name, data in payloads.items()
        },
    }
    assets = [
        {
            "id": index,
            "name": name,
            "size": len(data),
            "state": "uploaded",
            "digest": "sha256:" + hashlib.sha256(data).hexdigest(),
            "browser_download_url": f"https://github.com/btajp/narumi/releases/download/v0.1.1/{name}",
        }
        for index, (name, data) in enumerate(payloads.items(), 1)
    ]
    release = {
        "tag_name": "v0.1.1",
        "draft": True,
        "prerelease": False,
        "target_commitish": sealed["commit"],
        "assets": assets,
    }
    monkeypatch.setattr(verifier, "verify_local", lambda *args: sealed)
    monkeypatch.setattr(verifier, "check_history", lambda *args, **kwargs: None)
    monkeypatch.setattr(verifier, "api", lambda endpoint: release)
    monkeypatch.setattr(verifier, "list_releases", lambda: [release])
    monkeypatch.setattr(verifier, "remote_tags", lambda *args: "")
    monkeypatch.setattr(verifier, "inventory", lambda *args: {})
    monkeypatch.setattr(verifier, "verify_signature", lambda *args: None)
    monkeypatch.setattr(verifier, "verify_public", lambda *args: None)
    monkeypatch.setattr(
        verifier,
        "validate_artifacts",
        lambda *args: {key: sealed[key] for key in ("assets", "info", "signature")},
    )
    downloaded = []

    def fake_run(*command, output=None):
        assert command[:2] == ("gh", "api") and output is not None
        identifier = int(command[2].rsplit("/", 1)[-1])
        data = list(payloads.values())[identifier - 1]
        output.write_bytes(data)
        downloaded.append(output.name)
        return b""

    monkeypatch.setattr(verifier, "run", fake_run)

    def invoke(published=False):
        return verifier.verify_remote(
            tmp_path, tmp_path, "0.1.1", tmp_path, "jp.btajp.narumi", published=published
        )

    return release, sealed, downloaded, invoke


def test_draft_downloads_and_hashes_both_assets(remote):
    _, _, downloaded, invoke = remote
    assert invoke()["verified"] is True
    assert downloaded == ["narumi-0.1.1.zip", "appcast.xml"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("tag_name", "v0.1.0"),
        ("draft", False),
        ("draft", None),
        ("prerelease", True),
        ("target_commitish", "main"),
        ("target_commitish", "2" * 40),
    ],
)
def test_draft_identity_mismatch_rejected(verifier, remote, field, value):
    release, _, downloaded, invoke = remote
    release[field] = value
    with pytest.raises(verifier.ReleaseError):
        invoke()
    assert downloaded == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("state", "new"),
        ("size", 999),
        ("browser_download_url", "https://example.invalid/file"),
        ("digest", "sha256:" + "0" * 64),
        ("id", "1"),
        ("id", -1),
    ],
)
def test_remote_asset_metadata_mismatch_rejected(verifier, remote, field, value):
    release, _, downloaded, invoke = remote
    release["assets"][0][field] = value
    with pytest.raises(verifier.ReleaseError):
        invoke()
    assert downloaded == []


def test_extra_remote_asset_rejected(verifier, remote):
    release, _, downloaded, invoke = remote
    release["assets"].append({"name": "release.json"})
    with pytest.raises(verifier.ReleaseError):
        invoke()
    assert downloaded == []


def test_wrong_tag_target_rejected(verifier, monkeypatch, remote):
    monkeypatch.setattr(verifier, "remote_tags", lambda *args: "2" * 40 + "\trefs/tags/v0.1.1")
    with pytest.raises(verifier.ReleaseError):
        remote[-1]()


def test_published_requires_matching_tag_and_latest(verifier, monkeypatch, remote):
    release, sealed, _, invoke = remote
    release["draft"] = False
    with pytest.raises(verifier.ReleaseError):
        invoke(published=True)
    monkeypatch.setattr(
        verifier,
        "remote_tags",
        lambda *args: (
            "2" * 40 + "\trefs/tags/v0.1.1\n" + sealed["commit"] + "\trefs/tags/v0.1.1^{}"
        ),
    )
    assert invoke(published=True)["verified"] is True
    monkeypatch.setattr(
        verifier,
        "api",
        lambda endpoint: {"tag_name": "v0.0.9"} if endpoint.endswith("/latest") else release,
    )
    with pytest.raises(verifier.ReleaseError):
        invoke(published=True)


def test_published_requires_anonymous_check_even_if_authenticated_assets_pass(
    verifier, monkeypatch, remote
):
    release, sealed, downloaded, invoke = remote
    release["draft"] = False
    monkeypatch.setattr(
        verifier, "remote_tags", lambda *args: sealed["commit"] + "\trefs/tags/v0.1.1"
    )

    def inaccessible(*args):
        raise verifier.ReleaseError("anonymous HTTP 404")

    monkeypatch.setattr(verifier, "verify_public", inaccessible)
    with pytest.raises(verifier.ReleaseError, match="anonymous HTTP 404"):
        invoke(published=True)
    assert downloaded == ["narumi-0.1.1.zip", "appcast.xml"]


def test_draft_does_not_require_public_reachability(verifier, monkeypatch, remote):
    def anonymous_forbidden(*args):
        raise AssertionError("Drafts have no public download URL")

    monkeypatch.setattr(verifier, "verify_public", anonymous_forbidden)
    result = remote[-1]()
    assert result["verified"] is True and result["public_verified"] is False


def test_source_snapshot_rejects_fork_main_even_if_sha_exists_upstream(
    verifier, monkeypatch, tmp_path
):
    (tmp_path / "VERSION").write_text("0.1.1\n")
    candidate = "1" * 40
    commands = []

    def fake_git(root, *args):
        if args == ("rev-parse", "--abbrev-ref", "HEAD"):
            return "main"
        if args in (("rev-parse", "HEAD"), ("rev-parse", "origin/main")):
            return candidate
        if args == ("status", "--porcelain") or args[0] == "fetch":
            return ""
        raise AssertionError(args)

    def fake_api(endpoint):
        commands.append(endpoint)
        # Exactly the vulnerable case: candidate is API-reachable, but not upstream main.
        return {"sha": "2" * 40 if endpoint == "commits/main" else candidate}

    monkeypatch.setattr(verifier, "git", fake_git)
    monkeypatch.setattr(verifier, "api", fake_api)
    with pytest.raises(verifier.ReleaseError, match="公開先の main"):
        verifier.source_snapshot(tmp_path, "0.1.1")
    assert commands == ["commits/main"]


def test_remote_tags_are_queried_from_canonical_repository(verifier, monkeypatch, tmp_path):
    commands = []

    def fake_run(*command):
        commands.append(command)
        return ("2" * 40 + "\trefs/tags/v0.1.1\n").encode()

    monkeypatch.setattr(verifier, "run", fake_run)
    assert verifier.remote_tags(tmp_path, "0.1.1").startswith("2" * 40)
    assert commands == [
        (
            "git",
            "-C",
            str(tmp_path),
            "ls-remote",
            "--tags",
            "https://github.com/btajp/narumi.git",
            "refs/tags/v0.1.1",
            "refs/tags/v0.1.1^{}",
        )
    ]


def test_verify_draft_succeeds_when_published_tag_endpoint_is_404(verifier, monkeypatch, remote):
    def published_endpoint_404(endpoint):
        raise verifier.ReleaseError("HTTP 404: tag endpoint only exposes published releases")

    monkeypatch.setattr(verifier, "api", published_endpoint_404)
    _, _, downloaded, invoke = remote
    assert invoke()["verified"] is True
    assert downloaded == ["narumi-0.1.1.zip", "appcast.xml"]


@pytest.mark.parametrize("entries", [0, 2])
def test_draft_selection_requires_one_matching_tag(verifier, monkeypatch, remote, entries):
    release, _, downloaded, invoke = remote
    monkeypatch.setattr(verifier, "list_releases", lambda: [dict(release) for _ in range(entries)])
    with pytest.raises(verifier.ReleaseError):
        invoke()
    assert downloaded == []


def test_draft_selection_rejects_same_tag_even_if_only_one_entry_is_a_draft(
    verifier, monkeypatch, remote
):
    release, _, downloaded, invoke = remote
    monkeypatch.setattr(verifier, "list_releases", lambda: [release, {**release, "draft": False}])
    with pytest.raises(verifier.ReleaseError):
        invoke()
    assert downloaded == []


def test_draft_selection_reads_all_authenticated_release_pages(verifier, monkeypatch):
    draft = {"tag_name": "v0.1.1", "draft": True}
    commands = []

    def fake_run(*command):
        commands.append(command)
        return json.dumps([[{"tag_name": "v0.1.0", "draft": False}], [draft]]).encode()

    monkeypatch.setattr(verifier, "run", fake_run)
    assert verifier.release_for_verification("0.1.1", published=False) == draft
    assert commands == [("gh", "api", "repos/btajp/narumi/releases", "--paginate", "--slurp")]


def test_published_selection_uses_the_tag_endpoint(verifier, monkeypatch):
    endpoints = []

    def fake_api(endpoint):
        endpoints.append(endpoint)
        return {"tag_name": "v0.1.1", "draft": False}

    monkeypatch.setattr(verifier, "api", fake_api)
    assert verifier.release_for_verification("0.1.1", published=True)["draft"] is False
    assert endpoints == ["releases/tags/v0.1.1"]
