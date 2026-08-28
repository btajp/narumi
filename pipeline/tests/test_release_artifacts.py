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


class _IntegerSubclass(int):
    pass


def _sealed_assets(version: str, schema: int) -> dict:
    names = [f"narumi-{version}.zip", "appcast.xml"]
    if schema == 2:
        names.append(f"narumi-{version}.dmg")
    return {
        name: {"sha256": hashlib.sha256(name.encode()).hexdigest(), "size": 1} for name in names
    }


@pytest.mark.parametrize(
    ("version", "schema"),
    [
        ("0.0.0", 1),
        ("0.0.99", 1),
        ("0.1.0", 1),
        ("0.1.3", 1),
        ("0.1.4", 2),
        ("0.1.10", 2),
        ("0.2.0", 2),
        ("1.0.0", 2),
    ],
)
def test_release_schema_and_asset_names_follow_the_version_boundary(
    version: str, schema: int
) -> None:
    expected = (f"narumi-{version}.zip", "appcast.xml")
    assert release.release_schema(version) == schema
    assert release.validate_release_schema(version, schema) is None
    assert release.asset_names(version) == expected
    assert release.installer_name(version) == f"narumi-{version}.dmg"
    if schema == 2:
        expected += (f"narumi-{version}.dmg",)
    assert release.release_asset_names(version) == expected
    assert release.validate_sealed_assets(version, schema, _sealed_assets(version, schema)) is None


@pytest.mark.parametrize(("version", "schema"), [("0.1.3", 1), ("0.1.4", 2)])
@pytest.mark.parametrize(
    "invalid",
    [True, False, None, "1", "2", 1.0, 2.0, -1, 0, 3, _IntegerSubclass(1), _IntegerSubclass(2)],
)
def test_release_schema_rejects_unknown_values_and_integer_impostors(
    version: str, schema: int, invalid: object
) -> None:
    with pytest.raises(release.ReleaseError):
        release.validate_release_schema(version, invalid)
    with pytest.raises(release.ReleaseError):
        release.validate_sealed_assets(version, invalid, _sealed_assets(version, schema))


@pytest.mark.parametrize(
    ("version", "schema"), [("0.1.3", 2), ("0.1.4", 1), ("0.1.10", 1), ("1.0.0", 1)]
)
def test_release_schema_cannot_be_upgraded_or_downgraded_independently(
    version: str, schema: int
) -> None:
    with pytest.raises(release.ReleaseError):
        release.validate_release_schema(version, schema)
    with pytest.raises(release.ReleaseError):
        release.validate_sealed_assets(version, schema, _sealed_assets(version, schema))


@pytest.mark.parametrize(
    "version", ["v0.1.4", "0.1", "0.1.04", "0.1.4-beta.1", "0.1.4+build", "0.1.4\n", "０.1.4"]
)
def test_new_release_helpers_preserve_strict_stable_version_validation(
    tmp_path: Path, version: str
) -> None:
    calls = [
        lambda: release.release_schema(version),
        lambda: release.validate_release_schema(version, 2),
        lambda: release.installer_name(version),
        lambda: release.release_asset_names(version),
        lambda: release.asset_path(tmp_path, version, "appcast.xml"),
        lambda: release.validate_installer(tmp_path, version),
        lambda: release.validate_sealed_assets(version, 2, {}),
    ]
    for call in calls:
        with pytest.raises(release.ReleaseError, match="semver"):
            call()


@pytest.mark.parametrize(("version", "schema"), [("0.1.3", 1), ("0.1.4", 2)])
def test_asset_paths_route_without_creating_files(
    tmp_path: Path, version: str, schema: int
) -> None:
    directory = tmp_path / "not-created"
    for name in _sealed_assets(version, schema):
        folder = "installer" if name.endswith(".dmg") else "feed"
        assert release.asset_path(directory, version, name) == directory / folder / name
    assert not directory.exists()


@pytest.mark.parametrize(
    "name",
    [
        "../appcast.xml",
        "/tmp/appcast.xml",
        "feed/appcast.xml",
        "installer/narumi-0.1.4.dmg",
        "..\\appcast.xml",
        ".",
        "",
        "APPCAST.XML",
        "narumi-0.1.5.zip",
        "narumi-0.1.5.dmg",
        "narumi-0.1.4.zip?download=1",
        None,
        1,
    ],
)
def test_asset_paths_reject_names_outside_the_release(tmp_path: Path, name: object) -> None:
    with pytest.raises(release.ReleaseError):
        release.asset_path(tmp_path, "0.1.4", name)
    assert list(tmp_path.iterdir()) == []


def test_legacy_release_has_no_installer_path(tmp_path: Path) -> None:
    with pytest.raises(release.ReleaseError):
        release.asset_path(tmp_path, "0.1.3", "narumi-0.1.3.dmg")


@pytest.fixture
def installer(tmp_path: Path) -> Path:
    directory = tmp_path / "installer"
    directory.mkdir()
    (directory / "narumi-0.1.4.dmg").write_bytes(b"synthetic DMG content")
    return directory


def test_installer_reports_exact_hash_and_size_without_modification(installer: Path) -> None:
    path = installer / "narumi-0.1.4.dmg"
    original = path.read_bytes()
    assert release.validate_installer(installer, "0.1.4") == {
        "sha256": hashlib.sha256(original).hexdigest(),
        "size": len(original),
    }
    assert path.read_bytes() == original
    assert list(installer.iterdir()) == [path]


@pytest.mark.parametrize("version", ["0.0.99", "0.1.3"])
def test_legacy_release_cannot_validate_an_installer(installer: Path, version: str) -> None:
    (installer / "narumi-0.1.4.dmg").rename(installer / f"narumi-{version}.dmg")
    with pytest.raises(release.ReleaseError):
        release.validate_installer(installer, version)


@pytest.mark.parametrize("kind", ["missing", "file", "symlink"])
def test_installer_requires_a_real_directory(installer: Path, kind: str) -> None:
    path = installer.parent / "invalid-installer"
    if kind == "file":
        path.write_bytes(b"not a directory")
    elif kind == "symlink":
        path.symlink_to(installer, target_is_directory=True)
    with pytest.raises(release.ReleaseError):
        release.validate_installer(path, "0.1.4")


@pytest.mark.parametrize(
    "kind",
    [
        "missing",
        "empty",
        "directory",
        "symlink",
        "dangling-symlink",
        "wrong-version",
        "extra-file",
        "extra-directory",
    ],
)
def test_installer_requires_exactly_one_nonempty_regular_dmg(installer: Path, kind: str) -> None:
    path = installer / "narumi-0.1.4.dmg"
    if kind.startswith("extra-"):
        extra = installer / "unexpected"
        extra.mkdir() if kind == "extra-directory" else extra.write_bytes(b"extra")
    elif kind == "wrong-version":
        path.rename(installer / "narumi-0.1.5.dmg")
    else:
        original = installer.parent / "original.dmg"
        path.rename(original)
        if kind == "empty":
            path.write_bytes(b"")
        elif kind == "directory":
            path.mkdir()
        elif kind in ("symlink", "dangling-symlink"):
            path.symlink_to(original if kind == "symlink" else installer.parent / "missing.dmg")
    with pytest.raises(release.ReleaseError):
        release.validate_installer(installer, "0.1.4")


def test_dmg_remains_forbidden_in_the_two_file_update_feed(artifacts: Path) -> None:
    (artifacts / f"narumi-{VERSION}.dmg").write_bytes(b"synthetic DMG content")
    with pytest.raises(release.ReleaseError, match="ZIP と appcast"):
        release.validate_artifacts(artifacts, VERSION, BUILD, KEY)


@pytest.mark.parametrize(("version", "schema"), [("0.1.3", 1), ("0.1.4", 2)])
@pytest.mark.parametrize(
    "kind", ["missing-zip", "missing-feed", "dmg-schema", "extra", "wrong-version"]
)
def test_sealed_asset_keys_must_match_the_release_exactly(
    version: str, schema: int, kind: str
) -> None:
    assets = _sealed_assets(version, schema)
    if kind == "missing-zip":
        del assets[f"narumi-{version}.zip"]
    elif kind == "missing-feed":
        del assets["appcast.xml"]
    elif kind == "dmg-schema":
        if schema == 1:
            assets[f"narumi-{version}.dmg"] = assets["appcast.xml"].copy()
        else:
            del assets[f"narumi-{version}.dmg"]
    elif kind == "extra":
        assets["unexpected.json"] = assets["appcast.xml"].copy()
    else:
        assets["narumi-9.9.9.zip"] = assets.pop(f"narumi-{version}.zip")
    with pytest.raises(release.ReleaseError):
        release.validate_sealed_assets(version, schema, assets)


@pytest.mark.parametrize("assets", [None, [], "assets", 1, {}])
def test_sealed_assets_require_a_complete_dictionary(assets: object) -> None:
    with pytest.raises(release.ReleaseError):
        release.validate_sealed_assets("0.1.4", 2, assets)


@pytest.mark.parametrize(
    "metadata",
    [
        None,
        [],
        {},
        {"size": 1},
        {"sha256": "a" * 64},
        {"sha256": "a" * 64, "size": 1, "etag": "unexpected"},
    ],
)
def test_sealed_metadata_requires_exactly_hash_and_size(metadata: object) -> None:
    assets = _sealed_assets("0.1.4", 2)
    assets["narumi-0.1.4.dmg"] = metadata
    with pytest.raises(release.ReleaseError):
        release.validate_sealed_assets("0.1.4", 2, assets)


@pytest.mark.parametrize(
    "digest",
    [None, 1, b"a" * 64, "", "a" * 63, "a" * 65, "A" * 64, "g" * 64, "a" * 64 + "\n", "ａ" * 64],
)
def test_sealed_hash_requires_exact_lowercase_hex(digest: object) -> None:
    assets = _sealed_assets("0.1.4", 2)
    assets["appcast.xml"]["sha256"] = digest
    with pytest.raises(release.ReleaseError):
        release.validate_sealed_assets("0.1.4", 2, assets)


@pytest.mark.parametrize("size", [None, True, False, 0, -1, 1.0, "1", _IntegerSubclass(1)])
def test_sealed_size_requires_a_positive_builtin_integer(size: object) -> None:
    assets = _sealed_assets("0.1.4", 2)
    assets["narumi-0.1.4.zip"]["size"] = size
    with pytest.raises(release.ReleaseError):
        release.validate_sealed_assets("0.1.4", 2, assets)
