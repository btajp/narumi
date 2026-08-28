"""Offline fake executables for release orchestration tests; never use host credentials/tools."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import plistlib
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

ROOT = Path(os.environ["FAKE_RELEASE_ROOT"])
STATE = ROOT / "fake-state"
STATE.mkdir(exist_ok=True)
PROGRAM = Path(sys.argv[0]).name
ARGS = sys.argv[1:]
MODE = os.environ.get("FAKE_RELEASE_MODE", "")
COMMIT = "1" * 40
KEY = base64.b64encode(b"k" * 32).decode()
SIGNATURE = base64.b64encode(b"s" * 64).decode()
SPARKLE = "{http://www.andymatuschak.org/xml-namespaces/sparkle}"
VERSION = "0.1.1"
BUILD = 25
with (STATE / "calls.jsonl").open("a") as stream:
    stream.write(json.dumps([PROGRAM, ARGS]) + "\n")


def emit(value):
    print(json.dumps(value))


def git_command():
    args = ARGS[2:]
    if args == ["rev-parse", "--abbrev-ref", "HEAD"]:
        print("feature" if MODE == "wrong_branch" else "main")
    elif args == ["status", "--porcelain"]:
        if MODE == "dirty" or (MODE == "dirty_after_build" and (STATE / "built").exists()):
            print(" M app/file.swift")
    elif args[:1] == ["rev-parse"]:
        stale = MODE == "stale" and args[-1] == "origin/main"
        changed = MODE == "head_after_build" and (STATE / "built").exists()
        print("2" * 40 if stale or changed else COMMIT)
    elif args[:1] == ["fetch"]:
        pass
    elif args == ["rev-list", "--count", "HEAD"]:
        print(BUILD)
    elif args[:1] == ["ls-files"]:
        sys.stdout.buffer.write(
            b"pipeline/src/narumi/__init__.py\0server/src/narumi_server/__init__.py\0"
        )
    elif args[:1] == ["ls-remote"]:
        if MODE == "canonical_tag_exists" and args[2] == "https://github.com/btajp/narumi.git":
            print(f"{'2' * 40}\trefs/tags/v{VERSION}")
        elif MODE == "tag_exists" or (MODE == "tag_race" and (STATE / "built").exists()):
            print(f"{COMMIT}\trefs/tags/v{VERSION}")
        elif (STATE / "published").exists():
            print(f"{COMMIT}\trefs/tags/v{VERSION}")
    elif args[:2] == ["tag", "--list"]:
        if MODE == "local_tag_exists":
            print(f"v{VERSION}")
    else:
        raise AssertionError((PROGRAM, args))


def make_release():
    assert "--draft" in ARGS
    assert ARGS[ARGS.index("--target") + 1] == COMMIT
    assert ARGS[ARGS.index("--repo") + 1] == "btajp/narumi"
    paths = [Path(ARGS[-2]), Path(ARGS[-1])]
    assert [p.name for p in paths] == [f"narumi-{VERSION}.zip", "appcast.xml"]
    assets = []
    for identifier, source in enumerate(paths, 1):
        content = source.read_bytes()
        (STATE / f"asset-{identifier}").write_bytes(content)
        assets.append(
            {
                "id": identifier,
                "name": source.name,
                "state": "uploaded",
                "size": len(content),
                "digest": "sha256:" + hashlib.sha256(content).hexdigest(),
                "browser_download_url": f"https://github.com/btajp/narumi/releases/download/v{VERSION}/{source.name}",
            }
        )
    (STATE / "release.json").write_text(
        json.dumps(
            {
                "tag_name": f"v{VERSION}",
                "target_commitish": COMMIT,
                "draft": True,
                "prerelease": False,
                "assets": assets,
            }
        )
    )
    if MODE == "remote_mutation":
        original = (STATE / "asset-2").read_bytes()
        (STATE / "asset-2").write_bytes(original.replace(b"15.0", b"14.0"))


def gh_command():
    if ARGS == ["auth", "status"]:
        return
    if ARGS[:2] == ["release", "create"]:
        make_release()
        return
    assert ARGS[0] == "api", ARGS
    if MODE == "gh_error":
        raise SystemExit(1)
    endpoint = ARGS[1]
    if endpoint.endswith("/commits/main"):
        emit({"sha": "2" * 40 if MODE == "fork_main" else COMMIT})
    elif "/commits/" in endpoint:
        emit({"sha": endpoint.rsplit("/", 1)[-1]})
    elif endpoint.endswith("/releases"):
        release = (
            json.loads((STATE / "release.json").read_text())
            if (STATE / "release.json").exists()
            else None
        )
        if MODE == "existing_release":
            release = {"tag_name": f"v{VERSION}", "draft": True}
        emit([[release] if release else []])
    elif "/releases/tags/" in endpoint or endpoint.endswith("/releases/latest"):
        release = json.loads((STATE / "release.json").read_text())
        if release["draft"]:
            print("HTTP 404: published release not found", file=sys.stderr)
            raise SystemExit(1)
        emit(release)
    elif "/releases/assets/" in endpoint:
        identifier = endpoint.rsplit("/", 1)[-1]
        sys.stdout.buffer.write((STATE / f"asset-{identifier}").read_bytes())
    else:
        raise AssertionError(ARGS)


def make_app():
    assert ARGS[:2] == ["--release", "--runtime"]
    assert ARGS[ARGS.index("--build-override") + 1] == str(BUILD)
    assert Path(os.environ["NARUMI_TRACKED_SOURCES"]).is_file()
    contents = Path(os.environ["DIST_DIR"]) / "narumi.app/Contents"
    contents.mkdir(parents=True)
    info = {
        "CFBundleIdentifier": "jp.btajp.narumi",
        "CFBundleShortVersionString": VERSION,
        "CFBundleVersion": str(BUILD),
        "LSMinimumSystemVersion": "15.0",
        "SUPublicEDKey": KEY,
        "SUFeedURL": "https://github.com/btajp/narumi/releases/latest/download/appcast.xml",
        "SUEnableAutomaticChecks": True,
        "SUAutomaticallyUpdate": False,
    }
    if MODE == "wrong_plist":
        info["CFBundleVersion"] = "1"
    (contents / "Info.plist").write_bytes(plistlib.dumps(info))
    (STATE / "built").touch()


def ditto_command():
    source, target = map(Path, ARGS[-2:])
    if "-c" in ARGS:
        assert "--norsrc" in ARGS and "--noextattr" in ARGS
        with zipfile.ZipFile(target, "x") as archive:
            for path in source.rglob("*"):
                if path.is_file():
                    archive.write(path, str(Path(source.name) / path.relative_to(source)))
    else:
        assert ARGS[:2] == ["-x", "-k"]
        with zipfile.ZipFile(source) as archive:
            archive.extractall(target)


def make_appcast():
    assert ARGS[ARGS.index("--maximum-deltas") + 1] == "0"
    path = Path(ARGS[ARGS.index("-o") + 1])
    archive = path.parent / f"narumi-{VERSION}.zip"
    root = ET.Element("rss", version="2.0")
    item = ET.SubElement(ET.SubElement(root, "channel"), "item")
    for name, value in (
        ("shortVersionString", VERSION),
        ("version", str(BUILD)),
        ("minimumSystemVersion", "15.0"),
        ("hardwareRequirements", "arm64"),
    ):
        ET.SubElement(item, SPARKLE + name).text = value
    url = ARGS[ARGS.index("--download-url-prefix") + 1] + archive.name
    if MODE == "tampered_feed":
        url = "https://example.invalid/other.zip"
    ET.SubElement(
        item,
        "enclosure",
        {
            "url": url,
            "length": str(archive.stat().st_size),
            "type": "application/octet-stream",
            SPARKLE + "edSignature": SIGNATURE,
        },
    )
    ET.ElementTree(root).write(path, encoding="UTF-8", xml_declaration=True)


if PROGRAM == "git":
    git_command()
elif PROGRAM == "gh":
    gh_command()
elif PROGRAM == "build-app.sh":
    make_app()
elif PROGRAM == "ditto":
    ditto_command()
elif PROGRAM == "xcrun":
    if ARGS[:2] == ["notarytool", "submit"]:
        emit({"status": "Invalid" if MODE == "notary_rejected" else "Accepted"})
    else:
        assert ARGS[0] == "stapler"
        if MODE == "lost_ticket" and "verify-unpacked" in ARGS[-1]:
            raise SystemExit(1)
elif PROGRAM == "bundle_inventory.py":
    assert "--require-runtime" in ARGS and "--tracked-sources" in ARGS
    assert Path(ARGS[ARGS.index("--tracked-sources") + 1]).read_bytes().endswith(b"\0")
    if MODE == "inventory_failure" and ARGS[0] == "check-zip":
        raise SystemExit(1)
    emit({"checked": ARGS[0]})
elif PROGRAM in ("generate_keys", "sign_update", "generate_appcast"):
    assert ARGS[ARGS.index("--account") + 1] == os.environ.get(
        "SPARKLE_KEY_ACCOUNT", "jp.btajp.narumi"
    )
    if PROGRAM == "generate_keys":
        assert "-p" in ARGS and "-x" not in ARGS and "-f" not in ARGS
        print(base64.b64encode(b"w" * 32).decode() if MODE == "wrong_key" else KEY)
    elif PROGRAM == "generate_appcast":
        make_appcast()
    elif "--verify" in ARGS:
        if MODE == "verification_failure":
            raise SystemExit(1)
        assert ARGS[-1] == SIGNATURE
    else:
        assert "-p" in ARGS
        print(SIGNATURE)
elif PROGRAM not in ("codesign", "spctl", "uv", "check-version.sh"):
    raise AssertionError(PROGRAM)
