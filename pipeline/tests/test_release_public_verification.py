"""Verify the real anonymous download path using synthetic HTTP responses and ZIPs."""

from __future__ import annotations

import base64
import hashlib
import importlib
import importlib.util
import io
import plistlib
import urllib.error
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPARKLE = "{http://www.andymatuschak.org/xml-namespaces/sparkle}"
VERSION = "0.1.1"
ARCHIVE_NAME = "narumi-0.1.1.zip"
ARCHIVE_URL = "https://github.com/btajp/narumi/releases/download/v0.1.1/" + ARCHIVE_NAME
FEED_URL = "https://github.com/btajp/narumi/releases/latest/download/appcast.xml"
KEY = base64.b64encode(b"k" * 32).decode()
SIGNATURE = base64.b64encode(b"s" * 64).decode()
DMG_VERSION = "0.1.4"
DMG_NAME = "narumi-0.1.4.dmg"
DMG_URL = "https://github.com/btajp/narumi/releases/download/v0.1.4/" + DMG_NAME
INVENTORY_SHA256 = "a" * 64


def make_zip(key=KEY, *, version=VERSION):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr(
            "narumi.app/Contents/Info.plist",
            plistlib.dumps(
                {
                    "CFBundleIdentifier": "jp.btajp.narumi",
                    "CFBundleShortVersionString": version,
                    "CFBundleVersion": "25",
                    "LSMinimumSystemVersion": "15.0",
                    "SUFeedURL": FEED_URL,
                    "SUPublicEDKey": key,
                    "SUEnableAutomaticChecks": True,
                    "SUAutomaticallyUpdate": False,
                }
            ),
        )
    return stream.getvalue()


def make_feed(size, url=ARCHIVE_URL, *, version=VERSION):
    root = ET.Element("rss", version="2.0")
    item = ET.SubElement(ET.SubElement(root, "channel"), "item")
    for name, value in (
        ("version", "25"),
        ("shortVersionString", version),
        ("minimumSystemVersion", "15.0"),
        ("hardwareRequirements", "arm64"),
    ):
        ET.SubElement(item, SPARKLE + name).text = value
    ET.SubElement(
        item,
        "enclosure",
        {
            "url": url,
            "length": str(size),
            "type": "application/octet-stream",
            SPARKLE + "edSignature": SIGNATURE,
        },
    )
    return ET.tostring(root)


class FakeResponse(io.BytesIO):
    def __init__(self, data):
        super().__init__(data)
        self.headers = {"Content-Length": str(len(data))}

    def getcode(self):
        return 200

    def geturl(self):
        return "https://release-assets.githubusercontent.com/fixture"


def make_public_release(monkeypatch, tmp_path, version, installer_checks):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    spec = importlib.util.spec_from_file_location(
        "public_verify_test", ROOT / "scripts/release_verify.py"
    )
    assert spec is not None and spec.loader is not None
    verifier = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(verifier)
    public = importlib.import_module("release_public")

    def forbidden(*args, **kwargs):
        raise AssertionError("Real network, command, and global opener use is forbidden")

    monkeypatch.setattr(verifier.subprocess, "run", forbidden)
    monkeypatch.setattr(public.urllib.request.OpenerDirector, "open", forbidden)
    monkeypatch.setattr(public.urllib.request, "urlopen", forbidden)
    monkeypatch.setattr(public.ssl, "create_default_context", lambda: object())
    schema = {"0.1.1": 1, "0.1.3": 1, "0.1.4": 2}[version]
    archive_name = f"narumi-{version}.zip"
    archive_url = f"https://github.com/btajp/narumi/releases/download/v{version}/{archive_name}"
    archive = make_zip(version=version)
    payloads = {
        archive_url: archive,
        FEED_URL: make_feed(len(archive), archive_url, version=version),
    }
    original = tmp_path / "feed"
    original.mkdir()
    (original / archive_name).write_bytes(archive)
    (original / "appcast.xml").write_bytes(payloads[FEED_URL])
    sealed = {
        "schema_version": schema,
        "version": version,
        "build": 25,
        "public_key": KEY,
        **verifier.validate_artifacts(original, version, 25, KEY),
    }
    if schema == 2:
        payloads[DMG_URL] = b"synthetic installer DMG"
        (tmp_path / "installer").mkdir()
        (tmp_path / "installer" / DMG_NAME).write_bytes(payloads[DMG_URL])
        reseal_asset(sealed, DMG_NAME, payloads[DMG_URL])
        sealed["installer"] = {
            "notarization": {"id": "11111111-1111-1111-1111-111111111111", "status": "Accepted"},
            "app_inventory_sha256": INVENTORY_SHA256,
        }
    requests = []
    post_checks = []

    class FakeOpener:
        def open(self, request, timeout):
            requests.append(request.full_url)
            assert not (
                {"authorization", "cookie", "proxy-authorization"}
                & {name.lower() for name, _ in request.header_items()}
            )
            payload = payloads[request.full_url]
            if isinstance(payload, Exception):
                raise payload
            return FakeResponse(payload)

    monkeypatch.setattr(public.urllib.request, "build_opener", lambda *handlers: FakeOpener())
    monkeypatch.setattr(verifier, "inventory", lambda *args: post_checks.append("inventory"))
    monkeypatch.setattr(verifier, "verify_signature", lambda *args: post_checks.append("signature"))

    def check_installer(root, directory, artifacts, actual_version, expected):
        assert schema == 2
        assert root == directory == tmp_path
        assert actual_version == version == DMG_VERSION
        assert requests == [FEED_URL, archive_url, DMG_URL]
        assert post_checks == ["inventory", "signature"]
        assert artifacts != directory
        assert {path.name for path in artifacts.iterdir()} == {"feed", "installer"}
        assert {path.name for path in (artifacts / "feed").iterdir()} == {
            archive_name,
            "appcast.xml",
        }
        assert {path.name for path in (artifacts / "installer").iterdir()} == {DMG_NAME}
        assert expected == sealed["assets"][DMG_NAME]
        for name, metadata in sealed["assets"].items():
            folder = "installer" if name == DMG_NAME else "feed"
            content = (artifacts / folder / name).read_bytes()
            assert len(content) == metadata["size"]
            assert hashlib.sha256(content).hexdigest() == metadata["sha256"]
        installer_checks.append({"artifacts": artifacts, "expected": expected.copy()})
        post_checks.append("installer")
        return {**expected, "app_inventory_sha256": INVENTORY_SHA256}

    monkeypatch.setattr(verifier, "verify_installer", check_installer)

    def invoke():
        verifier.verify_public(tmp_path, tmp_path, sealed, tmp_path, "jp.btajp.narumi")

    return verifier, payloads, sealed, requests, post_checks, invoke


@pytest.fixture
def installer_checks():
    return []


@pytest.fixture
def public_release(monkeypatch, tmp_path, request, installer_checks):
    version = getattr(request, "param", VERSION)
    return make_public_release(monkeypatch, tmp_path, version, installer_checks)


@pytest.fixture
def dmg_release(monkeypatch, tmp_path, installer_checks):
    return make_public_release(monkeypatch, tmp_path, DMG_VERSION, installer_checks)


def reseal_asset(sealed, name, content):
    sealed["assets"][name] = {"size": len(content), "sha256": hashlib.sha256(content).hexdigest()}


def test_public_verification_follows_fixed_feed_then_validated_enclosure(public_release):
    _, _, _, requests, checks, invoke = public_release
    invoke()
    assert requests == [FEED_URL, ARCHIVE_URL]
    assert checks == ["inventory", "signature"]


@pytest.mark.parametrize("url", [FEED_URL, ARCHIVE_URL])
def test_public_404_fails_even_for_otherwise_valid_release(public_release, url):
    verifier, payloads, _, requests, checks, invoke = public_release
    payloads[url] = urllib.error.HTTPError(url, 404, "not public", {}, None)
    with pytest.raises(verifier.ReleaseError, match="HTTP 404"):
        invoke()
    assert requests[-1] == url
    assert checks == []


@pytest.mark.parametrize("url", [FEED_URL, ARCHIVE_URL])
def test_public_hash_mismatch_fails(public_release, url):
    verifier, payloads, _, _, checks, invoke = public_release
    content = payloads[url]
    payloads[url] = bytes([content[0] ^ 1]) + content[1:]
    with pytest.raises(verifier.ReleaseError, match="SHA256"):
        invoke()
    assert checks == []


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1/secret",
        "https://unexpected.example/asset.zip",
        "https://github.com/someone/other/releases/download/v0.1.1/asset.zip",
    ],
)
def test_unexpected_enclosure_is_rejected_before_fetch(public_release, url):
    verifier, payloads, sealed, requests, checks, invoke = public_release
    feed = make_feed(len(payloads[ARCHIVE_URL]), url)
    payloads[FEED_URL] = feed
    reseal_asset(sealed, "appcast.xml", feed)
    with pytest.raises(verifier.ReleaseError, match="URL"):
        invoke()
    assert requests == [FEED_URL]
    assert checks == []


def test_public_zip_still_requires_embedded_public_key(public_release):
    verifier, payloads, sealed, _, checks, invoke = public_release
    wrong = make_zip(base64.b64encode(b"w" * 32).decode())
    assert len(wrong) == len(payloads[ARCHIVE_URL])
    payloads[ARCHIVE_URL] = wrong
    reseal_asset(sealed, ARCHIVE_NAME, wrong)
    with pytest.raises(verifier.ReleaseError, match="SUPublicEDKey"):
        invoke()
    assert checks == []


@pytest.mark.parametrize("public_release", ["0.1.3", "0.1.4"], indirect=True)
def test_public_downloads_match_the_legacy_and_installer_release_schemas(
    public_release, installer_checks
):
    _, _, sealed, requests, checks, invoke = public_release
    version = sealed["version"]
    zip_url = f"https://github.com/btajp/narumi/releases/download/v{version}/narumi-{version}.zip"
    invoke()
    if version == DMG_VERSION:
        assert requests == [FEED_URL, zip_url, DMG_URL]
        assert checks == ["inventory", "signature", "installer"]
        assert len(installer_checks) == 1
        assert installer_checks[0]["expected"] == sealed["assets"][DMG_NAME]
        assert not installer_checks[0]["artifacts"].exists()
    else:
        assert requests == [FEED_URL, zip_url]
        assert checks == ["inventory", "signature"]
        assert installer_checks == []


@pytest.mark.parametrize("failure", ["404", "hash", "truncated"])
def test_public_dmg_download_failure_prevents_installer_verification(
    dmg_release, installer_checks, failure
):
    verifier, payloads, _, requests, checks, invoke = dmg_release
    if failure == "404":
        payloads[DMG_URL] = urllib.error.HTTPError(DMG_URL, 404, "not public", {}, None)
        message = "HTTP 404"
    elif failure == "hash":
        payload = payloads[DMG_URL]
        payloads[DMG_URL] = bytes([payload[0] ^ 1]) + payload[1:]
        message = "SHA256"
    else:
        payloads[DMG_URL] = payloads[DMG_URL][:-1]
        message = "Content-Length"
    with pytest.raises(verifier.ReleaseError, match=message):
        invoke()
    assert requests == [FEED_URL, DMG_URL.replace(".dmg", ".zip"), DMG_URL]
    assert checks == []
    assert installer_checks == []


@pytest.mark.parametrize(
    "url",
    [
        DMG_URL,
        "https://github.com/btajp/narumi/releases/latest/download/narumi-0.1.4.dmg",
        "https://github.com/btajp/narumi/releases/download/v0.1.3/narumi-0.1.3.dmg",
    ],
)
def test_dmg_cannot_replace_the_sparkle_zip_enclosure(dmg_release, installer_checks, url):
    verifier, payloads, sealed, requests, checks, invoke = dmg_release
    zip_url = DMG_URL.replace(".dmg", ".zip")
    feed = make_feed(len(payloads[zip_url]), url, version=DMG_VERSION)
    payloads[FEED_URL] = feed
    reseal_asset(sealed, "appcast.xml", feed)
    with pytest.raises(verifier.ReleaseError, match="URL"):
        invoke()
    assert requests == [FEED_URL]
    assert checks == []
    assert installer_checks == []


@pytest.mark.parametrize(
    "mutation", ["downgrade", "unknown-schema", "missing-dmg", "extra-asset", "wrong-dmg-name"]
)
def test_invalid_installer_release_shape_is_rejected_before_any_download(
    dmg_release, installer_checks, mutation
):
    verifier, _, sealed, requests, checks, invoke = dmg_release
    if mutation == "downgrade":
        sealed["schema_version"] = 1
        del sealed["assets"][DMG_NAME]
        del sealed["installer"]
    elif mutation == "unknown-schema":
        sealed["schema_version"] = 3
    elif mutation == "missing-dmg":
        del sealed["assets"][DMG_NAME]
    elif mutation == "extra-asset":
        sealed["assets"]["narumi.delta"] = sealed["assets"][DMG_NAME].copy()
    else:
        sealed["assets"]["narumi-0.1.5.dmg"] = sealed["assets"].pop(DMG_NAME)
    with pytest.raises(verifier.ReleaseError):
        invoke()
    assert requests == []
    assert checks == []
    assert installer_checks == []


def test_installer_inventory_fingerprint_must_match_the_sealed_release(
    dmg_release, installer_checks, monkeypatch
):
    verifier, _, _, requests, checks, invoke = dmg_release
    original_check = verifier.verify_installer

    def mismatched_fingerprint(*args):
        result = original_check(*args)
        return {**result, "app_inventory_sha256": "b" * 64}

    monkeypatch.setattr(verifier, "verify_installer", mismatched_fingerprint)
    with pytest.raises(verifier.ReleaseError, match="app inventory"):
        invoke()
    assert requests == [FEED_URL, DMG_URL.replace(".dmg", ".zip"), DMG_URL]
    assert checks == ["inventory", "signature", "installer"]
    assert len(installer_checks) == 1
    assert not installer_checks[0]["artifacts"].exists()


def test_installer_helper_failure_is_not_treated_as_a_success(
    dmg_release, installer_checks, monkeypatch
):
    verifier, _, _, _, checks, invoke = dmg_release
    original_check = verifier.verify_installer

    def fail_verification(*args):
        original_check(*args)
        raise verifier.ReleaseError("synthetic installer verification failure")

    monkeypatch.setattr(verifier, "verify_installer", fail_verification)
    with pytest.raises(verifier.ReleaseError, match="synthetic installer verification failure"):
        invoke()
    assert checks == ["inventory", "signature", "installer"]
    assert len(installer_checks) == 1
    assert not installer_checks[0]["artifacts"].exists()
