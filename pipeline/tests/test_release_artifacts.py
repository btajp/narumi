"""Release metadata gates use synthetic files, never signing keys or remote services."""

import base64
import hashlib
import importlib.util
import plistlib
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "narumi_release_artifacts", ROOT / "scripts/release_artifacts.py"
)
assert SPEC is not None and SPEC.loader is not None
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)

VERSION = "1.2.3"
BUILD = 42
KEY = base64.b64encode(b"K" * 32).decode()
SIGNATURE = base64.b64encode(b"S" * 64).decode()
SPARKLE = "{http://www.andymatuschak.org/xml-namespaces/sparkle}"
PLIST_NAME = "narumi.app/Contents/Info.plist"
ZIP_NAME = f"narumi-{VERSION}.zip"


def _plist(**changes: object) -> bytes:
    values = {
        "CFBundleIdentifier": "jp.btajp.narumi",
        "CFBundleShortVersionString": VERSION,
        "CFBundleVersion": str(BUILD),
        "LSMinimumSystemVersion": "15.0",
        "SUFeedURL": release.FEED_URL,
        "SUPublicEDKey": KEY,
        "SUEnableAutomaticChecks": True,
        "SUAutomaticallyUpdate": False,
    }
    values.update(changes)
    return plistlib.dumps(values)


def _feed(length: int = 1) -> ET.Element:
    root = ET.Element("rss", version="2.0")
    item = ET.SubElement(ET.SubElement(root, "channel"), "item")
    fields = {
        "shortVersionString": VERSION,
        "version": str(BUILD),
        "minimumSystemVersion": "15.0",
        "hardwareRequirements": "arm64",
    }
    for name, value in fields.items():
        ET.SubElement(item, f"{SPARKLE}{name}").text = value
    ET.SubElement(
        item,
        "enclosure",
        {
            "url": f"https://github.com/btajp/narumi/releases/download/v{VERSION}/{ZIP_NAME}",
            "length": str(length),
            "type": "application/octet-stream",
            f"{SPARKLE}edSignature": SIGNATURE,
        },
    )
    return root


def _write_feed(directory: Path, root: ET.Element | None = None) -> None:
    if root is None:
        root = _feed((directory / ZIP_NAME).stat().st_size)
    (directory / "appcast.xml").write_bytes(ET.tostring(root))


def _write_zip(directory: Path, entries: list[tuple[str, bytes]]) -> None:
    with zipfile.ZipFile(directory / ZIP_NAME, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in entries:
            archive.writestr(name, content)
    _write_feed(directory)


@pytest.fixture
def artifacts(tmp_path: Path) -> Path:
    directory = tmp_path / "release"
    directory.mkdir()
    _write_zip(directory, [(PLIST_NAME, _plist())])
    return directory


@pytest.mark.parametrize("value", ["0.0.0", "1.2.3", "12.34.567"])
def test_stable_version_is_parsed(value: str) -> None:
    assert release.version_tuple(value) == tuple(map(int, value.split(".")))
    assert release.asset_names(value) == (f"narumi-{value}.zip", "appcast.xml")


@pytest.mark.parametrize(
    "value",
    [
        "",
        "v1.2.3",
        "1.2",
        "1.2.3.4",
        "01.2.3",
        "1.02.3",
        "1.2.03",
        "1.2.3-beta.1",
        "1.2.3+build.1",
        " 1.2.3",
        "1.2.3\n",
        "１.2.3",
        "1.٢.3",
    ],
)
def test_only_canonical_stable_versions_are_accepted(value: str) -> None:
    with pytest.raises(release.ReleaseError, match="semver"):
        release.version_tuple(value)
    with pytest.raises(release.ReleaseError, match="semver"):
        release.asset_names(value)


@pytest.mark.parametrize("value", ["1", "42", "123456789"])
def test_positive_build_number_is_parsed(value: str) -> None:
    assert release.build_number(value) == int(value)


@pytest.mark.parametrize("value", ["", "0", "-1", "+1", "01", "1.0", "1e2", " 1", "1\n", "１"])
def test_build_number_rejects_noncanonical_values(value: str) -> None:
    with pytest.raises(release.ReleaseError, match="build"):
        release.build_number(value)


@pytest.mark.parametrize("size", [32, 64])
def test_key_and_signature_require_exact_canonical_base64(size: int) -> None:
    content = bytes(size)
    encoded = base64.b64encode(content).decode()
    assert release.decode_base64(encoded, size, "test") == content
    for invalid in (encoded + "\n", encoded + "=", encoded.rstrip("="), "!", "あ", ""):
        with pytest.raises(release.ReleaseError):
            release.decode_base64(invalid, size, "test")
    for wrong_length in (size - 1, size + 1):
        with pytest.raises(release.ReleaseError, match="長さ"):
            release.decode_base64(base64.b64encode(bytes(wrong_length)).decode(), size, "test")
    last_data = len(encoded.rstrip("=")) - 1
    noncanonical = encoded[:last_data] + "B" + encoded[last_data + 1 :]
    assert base64.b64decode(noncanonical, validate=True) == content
    with pytest.raises(release.ReleaseError, match="非正規"):
        release.decode_base64(noncanonical, size, "test")


def test_public_key_file_allows_line_wrapping_but_not_wrong_length(tmp_path: Path) -> None:
    path = tmp_path / "public-key.txt"
    path.write_text(f"  {KEY[:20]}\n{KEY[20:]}\n")
    assert release.public_key(path) == KEY
    path.write_text(SIGNATURE)
    with pytest.raises(release.ReleaseError, match="長さ"):
        release.public_key(path)


def test_valid_artifacts_report_exact_local_hashes_and_metadata(artifacts: Path) -> None:
    result = release.validate_artifacts(artifacts, VERSION, BUILD, KEY)
    assert result["signature"] == SIGNATURE
    assert result["info"]["CFBundleVersion"] == str(BUILD)
    assert result["info"]["SUPublicEDKey"] == KEY
    assert set(result["assets"]) == {ZIP_NAME, "appcast.xml"}
    for name, details in result["assets"].items():
        content = (artifacts / name).read_bytes()
        assert details == {"sha256": hashlib.sha256(content).hexdigest(), "size": len(content)}
    assert release.feed_version((artifacts / "appcast.xml").read_bytes()) == (VERSION, BUILD)


@pytest.mark.parametrize("attribute_form", [False, True])
def test_feed_versions_support_consistent_element_and_attribute_forms(attribute_form: bool) -> None:
    root = _feed()
    item = root.find("channel/item")
    assert item is not None
    enclosure = item.find("enclosure")
    assert enclosure is not None
    for name in ("version", "shortVersionString"):
        field = item.find(f"{SPARKLE}{name}")
        assert field is not None and field.text is not None
        enclosure.set(f"{SPARKLE}{name}", field.text)
        if attribute_form:
            item.remove(field)
    assert release.feed_version(ET.tostring(root)) == (VERSION, BUILD)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("url", "https://example.com/narumi.zip"),
        ("url", f"https://github.com/btajp/narumi/releases/download/v9.9.9/{ZIP_NAME}"),
        ("url", f"https://github.com/other/narumi/releases/download/v{VERSION}/{ZIP_NAME}"),
        ("length", "0"),
        ("type", "text/plain"),
        (f"{SPARKLE}edSignature", "invalid"),
        (f"{SPARKLE}edSignature", KEY),
    ],
)
def test_appcast_rejects_incorrect_enclosure_metadata(
    artifacts: Path, name: str, value: str
) -> None:
    root = ET.fromstring((artifacts / "appcast.xml").read_bytes())
    enclosure = root.find("channel/item/enclosure")
    assert enclosure is not None
    enclosure.set(name, value)
    _write_feed(artifacts, root)
    with pytest.raises(release.ReleaseError):
        release.validate_artifacts(artifacts, VERSION, BUILD, KEY)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("version", "43"),
        ("shortVersionString", "1.2.4"),
        ("minimumSystemVersion", "14.0"),
        ("hardwareRequirements", "x86_64"),
    ],
)
def test_appcast_rejects_incorrect_item_metadata(artifacts: Path, name: str, value: str) -> None:
    root = ET.fromstring((artifacts / "appcast.xml").read_bytes())
    field = root.find(f"channel/item/{SPARKLE}{name}")
    assert field is not None
    field.text = value
    _write_feed(artifacts, root)
    with pytest.raises(release.ReleaseError):
        release.validate_artifacts(artifacts, VERSION, BUILD, KEY)


def test_appcast_rejects_a_different_expected_signature(artifacts: Path) -> None:
    with pytest.raises(release.ReleaseError, match="署名.*不一致"):
        release.validate_appcast(
            artifacts / "appcast.xml",
            artifacts / ZIP_NAME,
            VERSION,
            BUILD,
            base64.b64encode(b"T" * 64).decode(),
        )


@pytest.mark.parametrize("name", ["version", "shortVersionString"])
@pytest.mark.parametrize("mutation", ["missing", "empty", "duplicate", "conflicting"])
def test_feed_version_fields_must_be_unambiguous(name: str, mutation: str) -> None:
    root = _feed()
    item = root.find("channel/item")
    assert item is not None
    enclosure = item.find("enclosure")
    field = item.find(f"{SPARKLE}{name}")
    assert enclosure is not None and field is not None
    if mutation == "missing":
        item.remove(field)
    elif mutation == "empty":
        field.text = None
    elif mutation == "duplicate":
        ET.SubElement(item, field.tag).text = field.text
    else:
        enclosure.set(field.tag, "999")
    with pytest.raises(release.ReleaseError):
        release.feed_version(ET.tostring(root))


@pytest.mark.parametrize("name", ["minimumSystemVersion", "hardwareRequirements"])
def test_appcast_rejects_duplicate_platform_fields(artifacts: Path, name: str) -> None:
    root = ET.fromstring((artifacts / "appcast.xml").read_bytes())
    item = root.find("channel/item")
    assert item is not None
    ET.SubElement(item, f"{SPARKLE}{name}").text = item.findtext(f"{SPARKLE}{name}")
    _write_feed(artifacts, root)
    with pytest.raises(release.ReleaseError):
        release.validate_artifacts(artifacts, VERSION, BUILD, KEY)


@pytest.mark.parametrize(
    "mutation",
    [
        "root",
        "missing-item",
        "extra-item",
        "extra-channel",
        "missing-enclosure",
        "extra-enclosure",
        "misplaced-enclosure",
        "delta",
    ],
)
def test_feed_rejects_ambiguous_or_unsupported_structure(mutation: str) -> None:
    root = _feed()
    channel = root.find("channel")
    item = root.find("channel/item")
    enclosure = root.find("channel/item/enclosure")
    assert channel is not None and item is not None and enclosure is not None
    if mutation == "root":
        root.tag = "feed"
    elif mutation == "missing-item":
        channel.remove(item)
    elif mutation == "extra-item":
        ET.SubElement(channel, "item")
    elif mutation == "extra-channel":
        ET.SubElement(root, "channel")
    elif mutation == "missing-enclosure":
        item.remove(enclosure)
    elif mutation == "extra-enclosure":
        ET.SubElement(channel, "enclosure")
    elif mutation == "misplaced-enclosure":
        item.remove(enclosure)
        channel.append(enclosure)
    else:
        ET.SubElement(item, f"{SPARKLE}deltas")
    with pytest.raises(release.ReleaseError):
        release.parse_feed(ET.tostring(root))


@pytest.mark.parametrize(
    "content",
    [
        b"<rss>",
        b"<!DOCTYPE rss><rss/>",
        b'<!DOCTYPE rss [<!ENTITY text "injected">]><rss>&text;</rss>',
        b" " * (2 * 1024 * 1024 + 1),
    ],
)
def test_feed_rejects_malformed_dtd_and_oversized_xml(content: bytes) -> None:
    with pytest.raises(release.ReleaseError):
        release.parse_feed(content)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("CFBundleIdentifier", "com.example.app"),
        ("CFBundleShortVersionString", "1.2.4"),
        ("CFBundleVersion", "43"),
        ("CFBundleVersion", BUILD),
        ("LSMinimumSystemVersion", "14.0"),
        ("SUFeedURL", "https://example.com/appcast.xml"),
        ("SUPublicEDKey", base64.b64encode(b"L" * 32).decode()),
        ("SUEnableAutomaticChecks", False),
        ("SUEnableAutomaticChecks", 1),
        ("SUAutomaticallyUpdate", True),
        ("SUAutomaticallyUpdate", 0),
    ],
)
def test_plist_requires_exact_release_identity_and_update_policy(name: str, value: object) -> None:
    with pytest.raises(release.ReleaseError):
        release.validate_plist(_plist(**{name: value}), VERSION, BUILD, KEY)


@pytest.mark.parametrize("binary", [False, True])
def test_plist_accepts_xml_and_binary_formats(binary: bool) -> None:
    data = _plist()
    if binary:
        data = plistlib.dumps(plistlib.loads(data), fmt=plistlib.FMT_BINARY)
    assert (
        release.validate_plist(data, VERSION, BUILD, KEY)["CFBundleIdentifier"] == "jp.btajp.narumi"
    )


@pytest.mark.parametrize(
    "data", [b"not a plist", b"<plist><dict>", b"bplist00broken", plistlib.dumps([])]
)
def test_plist_rejects_malformed_or_non_dictionary_content(data: bytes) -> None:
    with pytest.raises(release.ReleaseError):
        release.validate_plist(data, VERSION, BUILD, KEY)


@pytest.mark.parametrize("name", ["CFBundleVersion", "SUPublicEDKey", "SUFeedURL"])
def test_plist_rejects_duplicate_keys_even_when_values_agree(name: str) -> None:
    data = _plist()
    value = plistlib.loads(data)[name]
    duplicate = f"<key>{name}</key><string>{value}</string>".encode()
    data = data.replace(b"</dict>", duplicate + b"</dict>", 1)
    with pytest.raises(release.ReleaseError):
        release.validate_plist(data, VERSION, BUILD, KEY)


@pytest.mark.parametrize("name", ["source.json", "narumi.delta", "narumi-0.0.1.zip", ".DS_Store"])
def test_artifacts_reject_extra_assets(artifacts: Path, name: str) -> None:
    (artifacts / name).write_bytes(b"extra")
    with pytest.raises(release.ReleaseError, match="ZIP と appcast"):
        release.validate_artifacts(artifacts, VERSION, BUILD, KEY)


@pytest.mark.parametrize("name", [ZIP_NAME, "appcast.xml"])
@pytest.mark.parametrize("replacement", ["missing", "directory", "symlink"])
def test_artifacts_require_both_assets_to_be_regular_files(
    artifacts: Path, name: str, replacement: str
) -> None:
    target = artifacts / name
    original = artifacts.parent / name
    target.rename(original)
    if replacement == "directory":
        target.mkdir()
    elif replacement == "symlink":
        target.symlink_to(original)
    with pytest.raises(release.ReleaseError):
        release.validate_artifacts(artifacts, VERSION, BUILD, KEY)


def test_artifacts_reject_a_symlink_directory(artifacts: Path) -> None:
    link = artifacts.parent / "linked-release"
    link.symlink_to(artifacts, target_is_directory=True)
    with pytest.raises(release.ReleaseError, match="ディレクトリ"):
        release.validate_artifacts(link, VERSION, BUILD, KEY)


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "oversized", "corrupt"])
def test_archive_rejects_unsafe_or_missing_plist_metadata(artifacts: Path, mutation: str) -> None:
    if mutation == "missing":
        _write_zip(artifacts, [("other.app/Contents/Info.plist", _plist())])
    elif mutation == "duplicate":
        with pytest.warns(UserWarning, match="Duplicate name"):
            _write_zip(artifacts, [(PLIST_NAME, _plist()), (PLIST_NAME, _plist())])
    elif mutation == "oversized":
        _write_zip(artifacts, [(PLIST_NAME, _plist() + b" " * (1024 * 1024))])
    else:
        (artifacts / ZIP_NAME).write_bytes(b"not a zip")
        _write_feed(artifacts)
    with pytest.raises(release.ReleaseError):
        release.validate_artifacts(artifacts, VERSION, BUILD, KEY)


def test_archive_plist_is_validated_against_the_selected_release(artifacts: Path) -> None:
    _write_zip(artifacts, [(PLIST_NAME, _plist(CFBundleShortVersionString="9.9.9"))])
    with pytest.raises(release.ReleaseError, match="CFBundleShortVersionString"):
        release.validate_artifacts(artifacts, VERSION, BUILD, KEY)


def test_json_metadata_is_a_dictionary_and_never_overwritten(tmp_path: Path) -> None:
    path = tmp_path / "metadata.json"
    release.write_new_json(path, {"version": VERSION, "build": BUILD})
    assert release.load_json(path) == {"version": VERSION, "build": BUILD}
    original = path.read_bytes()
    with pytest.raises(FileExistsError):
        release.write_new_json(path, {"version": "9.9.9"})
    assert path.read_bytes() == original
    path.write_text("[]")
    with pytest.raises(release.ReleaseError):
        release.load_json(path)
