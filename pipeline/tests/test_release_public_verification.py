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


def make_zip(key=KEY):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr(
            "narumi.app/Contents/Info.plist",
            plistlib.dumps(
                {
                    "CFBundleIdentifier": "jp.btajp.narumi",
                    "CFBundleShortVersionString": VERSION,
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


def make_feed(size, url=ARCHIVE_URL):
    root = ET.Element("rss", version="2.0")
    item = ET.SubElement(ET.SubElement(root, "channel"), "item")
    for name, value in (
        ("version", "25"),
        ("shortVersionString", VERSION),
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


@pytest.fixture
def public_release(monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    spec = importlib.util.spec_from_file_location(
        "public_verify_test", ROOT / "scripts/release_verify.py"
    )
    verifier = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(verifier)
    public = importlib.import_module("release_public")

    def forbidden(*args, **kwargs):
        raise AssertionError("Real network, command, and global opener use is forbidden")

    monkeypatch.setattr(verifier.subprocess, "run", forbidden)
    monkeypatch.setattr(public.urllib.request.OpenerDirector, "open", forbidden)
    monkeypatch.setattr(public.urllib.request, "urlopen", forbidden)
    archive = make_zip()
    payloads = {ARCHIVE_URL: archive, FEED_URL: make_feed(len(archive))}
    original = tmp_path / "feed"
    original.mkdir()
    (original / ARCHIVE_NAME).write_bytes(archive)
    (original / "appcast.xml").write_bytes(payloads[FEED_URL])
    sealed = {
        "version": VERSION,
        "build": 25,
        "public_key": KEY,
        **verifier.validate_artifacts(original, VERSION, 25, KEY),
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

    def invoke():
        verifier.verify_public(tmp_path, tmp_path, sealed, tmp_path, "jp.btajp.narumi")

    return verifier, payloads, sealed, requests, post_checks, invoke


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
