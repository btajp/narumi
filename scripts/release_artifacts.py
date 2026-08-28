"""Strict local checks for Sparkle feed assets and versioned installer releases."""

from __future__ import annotations

import base64
import hashlib
import json
import plistlib
import re
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

REPOSITORY = "btajp/narumi"
DOWNLOAD_BASE = f"https://github.com/{REPOSITORY}/releases/download"
FEED_URL = f"https://github.com/{REPOSITORY}/releases/latest/download/appcast.xml"
SPARKLE = "{http://www.andymatuschak.org/xml-namespaces/sparkle}"
STABLE_VERSION = re.compile(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)", re.ASCII)


class ReleaseError(ValueError):
    """A release invariant failed; no remote mutation is safe."""


def require(condition: object, message: str) -> None:
    if not condition:
        raise ReleaseError(message)


def version_tuple(value: str) -> tuple[int, ...]:
    match = STABLE_VERSION.fullmatch(value)
    require(match, "安定版 semver（X.Y.Z、先頭ゼロなし）を指定してください")
    assert match is not None
    return tuple(map(int, match.groups()))


def build_number(value: str) -> int:
    require(re.fullmatch(r"[1-9][0-9]*", value), "build は単調増加の正整数が必要です")
    return int(value)


def decode_base64(value: str, size: int, label: str) -> bytes:
    try:
        decoded = base64.b64decode(value, validate=True)
    except (ValueError, TypeError) as exc:
        raise ReleaseError(f"{label} が不正です") from exc
    require(len(decoded) == size, f"{label} の長さが不正です")
    require(base64.b64encode(decoded).decode() == value, f"{label} が非正規形式です")
    return decoded


def public_key(path: Path) -> str:
    value = "".join(path.read_text().split())
    decode_base64(value, 32, "公開鍵")
    return value


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def asset_names(version: str) -> tuple[str, str]:
    version_tuple(version)
    return f"narumi-{version}.zip", "appcast.xml"


def release_schema(version: str) -> int:
    return 1 if version_tuple(version) < (0, 1, 4) else 2


def validate_release_schema(version: str, schema: object) -> None:
    require(
        type(schema) is int and schema == release_schema(version),
        "版に対応する release schema が不一致です",
    )


def installer_name(version: str) -> str:
    version_tuple(version)
    return f"narumi-{version}.dmg"


def release_asset_names(version: str) -> tuple[str, ...]:
    names = asset_names(version)
    return names if release_schema(version) == 1 else (*names, installer_name(version))


def asset_path(directory: Path, version: str, name: str) -> Path:
    require(name in release_asset_names(version), "許可されていない release asset 名です")
    folder = "feed" if name in asset_names(version) else "installer"
    return directory / folder / name


def validate_sealed_assets(version: str, schema: object, assets: object) -> None:
    validate_release_schema(version, schema)
    require(
        isinstance(assets, dict) and set(assets) == set(release_asset_names(version)),
        "封印した release assets が版に対応する配布物と一致しません",
    )
    for metadata in assets.values():
        require(
            isinstance(metadata, dict) and set(metadata) == {"sha256", "size"},
            "封印した asset metadata が不正です",
        )
        digest, size = metadata["sha256"], metadata["size"]
        require(
            isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest),
            "封印した asset SHA256 が不正です",
        )
        require(type(size) is int and size > 0, "封印した asset の長さが不正です")


def validate_installer(directory: Path, version: str) -> dict:
    require(release_schema(version) == 2, "この版では DMG を出荷しません")
    require(directory.is_dir() and not directory.is_symlink(), "installer directory が不正です")
    name = installer_name(version)
    require(
        {p.name for p in directory.iterdir()} == {name}, "installer の出荷対象は版付き DMG だけです"
    )
    path = directory / name
    require(path.is_file() and not path.is_symlink(), "DMG は通常ファイルが必要です")
    size = path.stat().st_size
    require(size > 0, "DMG が空です")
    return {"sha256": sha256(path), "size": size}


def parse_feed(content: bytes) -> tuple[ET.Element, ET.Element]:
    require(len(content) <= 2 * 1024 * 1024, "appcast が大きすぎます")
    require(b"<!DOCTYPE" not in content.upper(), "appcast の DTD は許可しません")
    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        raise ReleaseError("appcast XML が不正です") from exc
    require(root.tag == "rss", "appcast は RSS である必要があります")
    require(len(root.findall("channel")) == 1, "appcast の channel は 1 件だけにしてください")
    items = root.findall("channel/item")
    require(len(items) == 1, "appcast は今回の安定版 1 件だけにしてください")
    enclosures = list(root.iter("enclosure"))
    require(len(enclosures) == 1, "appcast の enclosure は ZIP 1 件だけにしてください")
    require(items[0].find("enclosure") is enclosures[0], "enclosure の位置が不正です")
    require(items[0].find(f"{SPARKLE}deltas") is None, "delta は出荷しません")
    return items[0], enclosures[0]


def feed_field(item: ET.Element, enclosure: ET.Element, name: str) -> str:
    elements = item.findall(f"{SPARKLE}{name}")
    require(len(elements) <= 1, f"appcast の {name} が重複しています")
    element_value = elements[0].text if elements else None
    attribute_value = enclosure.get(f"{SPARKLE}{name}")
    if element_value is not None and attribute_value is not None:
        require(element_value == attribute_value, f"appcast の {name} が矛盾しています")
    value = element_value if element_value is not None else attribute_value
    require(value, f"appcast に {name} がありません")
    assert value is not None
    return value


def feed_version(content: bytes) -> tuple[str, int]:
    item, enclosure = parse_feed(content)
    version = feed_field(item, enclosure, "shortVersionString")
    version_tuple(version)
    return version, build_number(feed_field(item, enclosure, "version"))


def validate_appcast(
    path: Path, archive: Path, version: str, build: int, signature: str | None = None
) -> str:
    item, enclosure = parse_feed(path.read_bytes())
    require(
        feed_field(item, enclosure, "shortVersionString") == version, "appcast の版が不一致です"
    )
    require(feed_field(item, enclosure, "version") == str(build), "appcast の build が不一致です")
    expected_url = f"{DOWNLOAD_BASE}/v{version}/{asset_names(version)[0]}"
    require(enclosure.get("url") == expected_url, "appcast のダウンロード URL が不一致です")
    require(enclosure.get("length") == str(archive.stat().st_size), "appcast の ZIP 長が不一致です")
    require(enclosure.get("type") == "application/octet-stream", "appcast の MIME type が不正です")
    for name, expected in (("minimumSystemVersion", "15.0"), ("hardwareRequirements", "arm64")):
        entries = item.findall(f"{SPARKLE}{name}")
        require(
            len(entries) == 1 and entries[0].text == expected, f"{name} が欠落・重複・不一致です"
        )
    actual_signature = enclosure.get(f"{SPARKLE}edSignature", "")
    decode_base64(actual_signature, 64, "EdDSA 署名")
    if signature is not None:
        require(actual_signature == signature, "ZIP の署名と appcast の署名が不一致です")
    return actual_signature


def validate_plist(data: bytes, version: str, build: int, key: str) -> dict:
    try:
        if not data.startswith(b"bplist"):
            root = ET.fromstring(data)
            keys = [element.text for element in root.findall("dict/key")]
            require(len(keys) == len(set(keys)), "Info.plist の key が重複しています")
        info = plistlib.loads(data)
    except ReleaseError:
        raise
    except (ValueError, plistlib.InvalidFileException, ET.ParseError) as exc:
        raise ReleaseError("Info.plist が不正です") from exc
    require(isinstance(info, dict), "Info.plist は dict である必要があります")
    expected = {
        "CFBundleIdentifier": "jp.btajp.narumi",
        "CFBundleShortVersionString": version,
        "CFBundleVersion": str(build),
        "LSMinimumSystemVersion": "15.0",
        "SUFeedURL": FEED_URL,
        "SUPublicEDKey": key,
    }
    for name, value in expected.items():
        require(info.get(name) == value, f"Info.plist の {name} が不一致です")
    require(info.get("SUEnableAutomaticChecks") is True, "自動更新確認が無効です")
    require(info.get("SUAutomaticallyUpdate") is False, "更新の無通知適用は許可しません")
    return expected


def validate_artifacts(directory: Path, version: str, build: int, key: str) -> dict:
    """Inspect metadata without extracting archives or trusting filenames from XML."""
    version_tuple(version)
    build_number(str(build))
    decode_base64(key, 32, "公開鍵")
    names = asset_names(version)
    require(directory.is_dir() and not directory.is_symlink(), "成果物ディレクトリが不正です")
    require(
        {p.name for p in directory.iterdir()} == set(names), "出荷対象は ZIP と appcast だけです"
    )
    for name in names:
        path = directory / name
        require(path.is_file() and not path.is_symlink(), "成果物は通常ファイルが必要です")
    archive, appcast = (directory / name for name in names)
    signature = validate_appcast(appcast, archive, version, build)
    try:
        with zipfile.ZipFile(archive) as zipped:
            matches = [
                i for i in zipped.infolist() if i.filename == "narumi.app/Contents/Info.plist"
            ]
            require(len(matches) == 1, "ZIP 内の Info.plist が欠落または重複しています")
            require(matches[0].file_size < 1024 * 1024, "ZIP 内の Info.plist が大きすぎます")
            info = validate_plist(zipped.read(matches[0]), version, build, key)
    except (zipfile.BadZipFile, RuntimeError) as exc:
        raise ReleaseError("リリース ZIP が不正です") from exc
    return {
        "info": info,
        "signature": signature,
        "assets": {
            name: {"sha256": sha256(directory / name), "size": (directory / name).stat().st_size}
            for name in names
        },
    }


def load_json(path: Path) -> dict:
    value = json.loads(path.read_text())
    require(isinstance(value, dict), "検証メタデータが不正です")
    return value


def write_new_json(path: Path, value: object) -> None:
    with path.open("x") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")
