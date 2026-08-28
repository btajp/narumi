#!/usr/bin/env python3
"""Seal local releases and re-download/verify drafts without publishing or replacing assets."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from release_artifacts import (
    DOWNLOAD_BASE,
    FEED_URL,
    REPOSITORY,
    ReleaseError,
    asset_names,
    build_number,
    feed_version,
    load_json,
    parse_feed,
    public_key,
    require,
    sha256,
    validate_appcast,
    validate_artifacts,
    validate_plist,
    version_tuple,
    write_new_json,
)
from release_public import download_public


def run(*command: str, output: Path | None = None) -> bytes:
    """Keep tool errors/arguments out of logs; never disclose credential-bearing output."""
    try:
        if output is None:
            result = subprocess.run(command, check=True, capture_output=True, timeout=300)
            return result.stdout
        with output.open("xb") as stream:
            subprocess.run(command, check=True, stdout=stream, stderr=subprocess.PIPE, timeout=300)
        return b""
    except (subprocess.SubprocessError, OSError) as exc:
        raise ReleaseError(f"{Path(command[0]).name} の検証コマンドに失敗しました") from exc


def git(root: Path, *args: str) -> str:
    return run("git", "-C", str(root), *args).decode().strip()


def api(endpoint: str) -> dict:
    value = json.loads(run("gh", "api", f"repos/{REPOSITORY}/{endpoint}"))
    require(isinstance(value, dict), "GitHub 応答が不正です")
    return value


def source_snapshot(root: Path, version: str, *, fetch: bool = True) -> tuple[dict, bytes]:
    version_tuple(version)
    require((root / "VERSION").read_text().strip() == version, "VERSION と指定版が不一致です")
    require(git(root, "rev-parse", "--abbrev-ref", "HEAD") == "main", "main からのみ出荷できます")
    require(not git(root, "status", "--porcelain"), "作業ツリーが clean ではありません")
    if fetch:
        git(root, "fetch", "--quiet", "origin", "main")
    commit = git(root, "rev-parse", "HEAD")
    require(re.fullmatch(r"[0-9a-f]{40}", commit), "commit SHA が不正です")
    require(commit == git(root, "rev-parse", "origin/main"), "HEAD と origin/main が不一致です")
    # A fork/PR commit may be reachable through the upstream API without being merged.
    # The canonical repository's current main, not just origin/main or SHA reachability,
    # must identify the exact candidate commit.
    require(api("commits/main").get("sha") == commit, "HEAD と公開先の main が不一致です")
    build = build_number(git(root, "rev-list", "--count", "HEAD"))
    tracked = run(
        "git",
        "-C",
        str(root),
        "ls-files",
        "-z",
        "--",
        "pipeline/src/narumi",
        "server/src/narumi_server",
    )
    require(tracked.endswith(b"\0"), "公開ソースの追跡一覧が空または不正です")
    snapshot = {
        "schema_version": 1,
        "repository": REPOSITORY,
        "version": version,
        "build": build,
        "commit": commit,
        "public_key": public_key(root / "app/sparkle-public-key.txt"),
        "tracked_sources_sha256": hashlib.sha256(tracked).hexdigest(),
    }
    require(git(root, "rev-parse", "HEAD") == commit, "検証中に HEAD が変わっています")
    require(
        git(root, "rev-parse", "--abbrev-ref", "HEAD") == "main", "検証中に branch が変わっています"
    )
    require(not git(root, "status", "--porcelain"), "検証中に作業ツリーが変わっています")
    return snapshot, tracked


def remote_tags(root: Path, version: str) -> str:
    return git(
        root,
        "ls-remote",
        "--tags",
        f"https://github.com/{REPOSITORY}.git",
        f"refs/tags/v{version}",
        f"refs/tags/v{version}^{{}}",
    )


def list_releases() -> list[dict]:
    """Authenticated listing includes drafts visible to the release operator."""
    pages = json.loads(run("gh", "api", f"repos/{REPOSITORY}/releases", "--paginate", "--slurp"))
    require(
        isinstance(pages, list) and all(isinstance(p, list) for p in pages),
        "Release 一覧が不正です",
    )
    releases = [release for page in pages for release in page]
    require(all(isinstance(release, dict) for release in releases), "Release 一覧の項目が不正です")
    return releases


def release_for_verification(version: str, *, published: bool) -> dict:
    if published:
        return api(f"releases/tags/v{version}")
    # The tag endpoint returns published releases, not drafts. Do not interpret a 404
    # there as a missing draft, or rely on an API behavior that differs by permissions.
    matches = [release for release in list_releases() if release.get("tag_name") == f"v{version}"]
    require(len(matches) == 1, "対象 tag の draft が欠落または同名 Release が複数あります")
    require(matches[0].get("draft") is True, "対象 tag の Release が draft ではありません")
    return matches[0]


def check_history(version: str, build: int, *, allow_current: bool = False) -> None:
    """Network errors and incomplete older feeds fail closed, rather than disabling monotonicity."""
    for release in list_releases():
        tag = release.get("tag_name", "")
        if tag == f"v{version}":
            require(allow_current, "同じ版の Release が既に存在します")
            continue
        if release.get("draft") or release.get("prerelease"):
            continue
        require(isinstance(tag, str) and tag.startswith("v"), "既存 Release の版を検証できません")
        previous_version = tag[1:]
        require(
            version_tuple(version) > version_tuple(previous_version), "既存版より新しい版が必要です"
        )
        feeds = [a for a in release.get("assets", []) if a.get("name") == "appcast.xml"]
        require(len(feeds) == 1, "既存 Release の appcast が欠落または重複しています")
        require(0 < feeds[0].get("size", 0) <= 2 * 1024 * 1024, "既存 appcast の長さが不正です")
        identifier = feeds[0].get("id")
        require(type(identifier) is int and identifier > 0, "既存 appcast の ID が不正です")
        content = run(
            "gh",
            "api",
            f"repos/{REPOSITORY}/releases/assets/{identifier}",
            "-H",
            "Accept: application/octet-stream",
        )
        feed_semver, previous_build = feed_version(content)
        require(feed_semver == previous_version, "既存 Release と appcast の版が不一致です")
        require(build > previous_build, "CFBundleVersion は既存 Release より大きくしてください")


def preflight(root: Path, directory: Path, version: str) -> dict:
    require(
        not directory.exists() and not directory.is_symlink(),
        "RELEASE_DIR は未使用の場所を指定してください",
    )
    require(directory.is_absolute(), "RELEASE_DIR は絶対パスを指定してください")
    resolved = directory.resolve()
    require(resolved not in (root.resolve(), Path.home(), Path("/")), "RELEASE_DIR が広すぎます")
    require(
        resolved.name not in ("dist", "Applications"),
        "RELEASE_DIR は専用の子ディレクトリが必要です",
    )
    snapshot, tracked = source_snapshot(root, version)
    require(not remote_tags(root, version), "同じ版の remote tag が既に存在します")
    require(not git(root, "tag", "--list", f"v{version}"), "同じ版の local tag が既に存在します")
    check_history(version, snapshot["build"])
    directory.mkdir(parents=True, mode=0o700)
    write_new_json(directory / "source.json", snapshot)
    with (directory / "tracked-sources.nul").open("xb") as stream:
        stream.write(tracked)
    return snapshot


def inventory(root: Path, directory: Path, target: Path, kind: str) -> object:
    output = run(
        sys.executable,
        str(root / "scripts/bundle_inventory.py"),
        f"check-{kind}",
        str(target),
        "--require-runtime",
        "--tracked-sources",
        str(directory / "tracked-sources.nul"),
    )
    return json.loads(output)


def context(root: Path, directory: Path, version: str, *, fetch: bool = True) -> dict:
    saved = load_json(directory / "source.json")
    current, tracked = source_snapshot(root, version, fetch=fetch)
    require(saved == current, "ビルド元 commit / 版 / build / 公開鍵が出荷準備時から変わっています")
    require(
        (directory / "tracked-sources.nul").read_bytes() == tracked,
        "公開ソース一覧が変更されています",
    )
    return saved


def check_app(root: Path, directory: Path, version: str) -> dict:
    snapshot = context(root, directory, version, fetch=False)
    app = directory / "build/narumi.app"
    validate_plist(
        (app / "Contents/Info.plist").read_bytes(),
        version,
        snapshot["build"],
        snapshot["public_key"],
    )
    return snapshot


def verify_signature(directory: Path, snapshot: dict, sparkle_bin: Path, account: str) -> None:
    require(account and not account.startswith("-"), "Sparkle account が不正です")
    run(
        str(sparkle_bin / "sign_update"),
        "--account",
        account,
        "--verify",
        str(directory / asset_names(snapshot["version"])[0]),
        snapshot["signature"],
    )


def seal(root: Path, directory: Path, version: str, sparkle_bin: Path, account: str) -> dict:
    snapshot = check_app(root, directory, version)
    require(
        load_json(directory / "notary-result.json").get("status") == "Accepted", "公証が未承認です"
    )
    details = validate_artifacts(
        directory / "feed", version, snapshot["build"], snapshot["public_key"]
    )
    expected_signature = (directory / "archive-signature.txt").read_text().strip()
    require(
        details["signature"] == expected_signature, "署名後に ZIP または appcast が変わっています"
    )
    snapshot.update(details)
    verify_signature(directory / "feed", snapshot, sparkle_bin, account)
    app_inventory = inventory(root, directory, directory / "build/narumi.app", "app")
    zip_inventory = inventory(root, directory, directory / "feed" / asset_names(version)[0], "zip")
    write_new_json(directory / "app-inventory.json", app_inventory)
    write_new_json(directory / "zip-inventory.json", zip_inventory)
    write_new_json(directory / "release.json", snapshot)
    return snapshot


def verify_local(
    root: Path, directory: Path, version: str, sparkle_bin: Path, account: str
) -> dict:
    original = context(root, directory, version)
    sealed = load_json(directory / "release.json")
    require(
        all(sealed.get(k) == v for k, v in original.items()), "封印したリリース情報が変わっています"
    )
    details = validate_artifacts(
        directory / "feed", version, original["build"], original["public_key"]
    )
    require(
        all(sealed.get(k) == v for k, v in details.items()),
        "ZIP / appcast の SHA256 が変わっています",
    )
    inventory(root, directory, directory / "feed" / asset_names(version)[0], "zip")
    verify_signature(directory / "feed", sealed, sparkle_bin, account)
    return sealed


def verify_remote(
    root: Path,
    directory: Path,
    version: str,
    sparkle_bin: Path,
    account: str,
    *,
    published: bool = False,
) -> dict:
    sealed = verify_local(root, directory, version, sparkle_bin, account)
    check_history(version, sealed["build"], allow_current=True)
    release = release_for_verification(version, published=published)
    require(release.get("tag_name") == f"v{version}", "Release tag が不一致です")
    require(
        release.get("draft") is (not published), "Release の draft / published 状態が不一致です"
    )
    require(release.get("prerelease") is False, "安定版 Release である必要があります")
    require(
        release.get("target_commitish") == sealed["commit"], "Release の対象 commit が不一致です"
    )
    tags = remote_tags(root, version)
    if tags:
        mapping = dict(row.split("\t")[::-1] for row in tags.splitlines())
        actual = mapping.get(f"refs/tags/v{version}^{{}}", mapping.get(f"refs/tags/v{version}"))
        require(actual == sealed["commit"], "remote tag が別 commit を参照しています")
    if published:
        require(tags, "公開済み Release の remote tag がありません")
        require(
            api("releases/latest").get("tag_name") == f"v{version}", "latest の更新先が不一致です"
        )
    assets = release.get("assets", [])
    require(
        len(assets) == 2 and {a.get("name") for a in assets} == set(sealed["assets"]),
        "GitHub の公開対象が ZIP と appcast の 2 ファイルと一致しません",
    )
    with tempfile.TemporaryDirectory(prefix="narumi-release-verify-") as temporary:
        downloaded = Path(temporary)
        for asset in assets:
            name = asset["name"]
            expected = sealed["assets"][name]
            require(asset.get("state") == "uploaded", "GitHub asset がアップロード未完了です")
            require(asset.get("size") == expected["size"], "GitHub asset の長さが不一致です")
            expected_url = f"{DOWNLOAD_BASE}/v{version}/{name}"
            require(
                asset.get("browser_download_url") == expected_url, "GitHub asset URL が不一致です"
            )
            digest = asset.get("digest")
            require(
                digest in (None, "sha256:" + expected["sha256"]), "GitHub asset digest が不一致です"
            )
            identifier = asset.get("id")
            require(type(identifier) is int and identifier > 0, "GitHub asset ID が不正です")
            target = downloaded / name
            run(
                "gh",
                "api",
                f"repos/{REPOSITORY}/releases/assets/{identifier}",
                "-H",
                "Accept: application/octet-stream",
                output=target,
            )
            require(
                target.stat().st_size == expected["size"] and sha256(target) == expected["sha256"],
                "再ダウンロードした asset の SHA256 / 長さが不一致です",
            )
        details = validate_artifacts(downloaded, version, sealed["build"], sealed["public_key"])
        require(
            all(sealed.get(k) == v for k, v in details.items()),
            "GitHub 成果物が出荷準備時と不一致です",
        )
        inventory(root, directory, downloaded / asset_names(version)[0], "zip")
        verify_signature(downloaded, sealed, sparkle_bin, account)
    if published:
        verify_public(root, directory, sealed, sparkle_bin, account)
    return {
        "version": version,
        "commit": sealed["commit"],
        "assets": sealed["assets"],
        "verified": True,
        "public_verified": published,
    }


def verify_public(
    root: Path, directory: Path, sealed: dict, sparkle_bin: Path, account: str
) -> None:
    """Check the same unauthenticated feed and enclosure that an installed Sparkle app uses."""
    version = sealed["version"]
    archive_name, feed_name = asset_names(version)
    with tempfile.TemporaryDirectory(prefix="narumi-public-release-verify-") as temporary:
        downloaded = Path(temporary)
        public_feed = downloaded / feed_name
        download_public(FEED_URL, public_feed, sealed["assets"][feed_name])
        # Validate the public feed before following any URL from its contents. The sealed
        # local ZIP supplies the already verified length until the public ZIP is downloaded.
        validate_appcast(
            public_feed,
            directory / "feed" / archive_name,
            version,
            sealed["build"],
            sealed["signature"],
        )
        _, enclosure = parse_feed(public_feed.read_bytes())
        download_public(
            enclosure.get("url"), downloaded / archive_name, sealed["assets"][archive_name]
        )
        details = validate_artifacts(downloaded, version, sealed["build"], sealed["public_key"])
        require(
            all(sealed.get(k) == v for k, v in details.items()),
            "匿名取得した公開成果物が出荷準備時と不一致です",
        )
        inventory(root, directory, downloaded / archive_name, "zip")
        verify_signature(downloaded, sealed, sparkle_bin, account)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=(
            "preflight",
            "check-app",
            "check-notary",
            "seal",
            "verify-local",
            "verify-ready",
            "verify-draft",
            "verify-published",
        ),
    )
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--release-dir", required=True, type=Path)
    parser.add_argument("--version", required=True)
    parser.add_argument("--sparkle-bin", type=Path)
    parser.add_argument("--account", default="jp.btajp.narumi")
    args = parser.parse_args()
    try:
        if args.command == "preflight":
            result = preflight(args.root, args.release_dir, args.version)
            print(f"{result['commit']}\t{result['build']}")
            return
        if args.command == "check-app":
            check_app(args.root, args.release_dir, args.version)
            print("release-verify: app metadata OK")
            return
        if args.command == "check-notary":
            require(
                load_json(args.release_dir / "notary-result.json").get("status") == "Accepted",
                "公証が未承認です",
            )
            print("release-verify: notarization accepted")
            return
        require(args.sparkle_bin is not None, "--sparkle-bin が必要です")
        common = args.root, args.release_dir, args.version, args.sparkle_bin, args.account
        if args.command == "seal":
            seal(*common)
        elif args.command in ("verify-local", "verify-ready"):
            sealed = verify_local(*common)
            if args.command == "verify-ready":
                require(
                    not remote_tags(args.root, args.version), "同じ版の remote tag が既に存在します"
                )
                require(
                    not git(args.root, "tag", "--list", f"v{args.version}"),
                    "同じ版の local tag が既に存在します",
                )
                check_history(args.version, sealed["build"])
                # History downloads/signature checks may take time. Check the source last too.
                context(args.root, args.release_dir, args.version, fetch=False)
        else:
            verify_remote(*common, published=args.command == "verify-published")
        print(f"release-verify: {args.command} OK (v{args.version})")
    except (ReleaseError, OSError, ValueError, KeyError, TypeError) as exc:
        print(f"release-verify: {exc}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
