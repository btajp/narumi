#!/usr/bin/env bash
# scripts/check-version.sh — VERSION ファイルを正本に、版の整合を検査する。
#
# 検査対象（すべて一致すること）:
#   VERSION / pipeline・server の pyproject.toml と Python __version__ /
#   uv.lock の workspace package / recorder の版 / CHANGELOG.md の最新見出し
#
# Usage: scripts/check-version.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

fail() {
  echo "check-version: $*" >&2
  exit 1
}

[[ -f "$ROOT/VERSION" ]] || fail "VERSION ファイルがありません"
version="$(tr -d '[:space:]' < "$ROOT/VERSION")"
[[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+([+.-][0-9A-Za-z.-]+)?$ ]] \
  || fail "VERSION が semver ではありません: '$version'"

[[ -f "$ROOT/uv.lock" ]] || fail "uv.lock がありません"
command -v python3 >/dev/null 2>&1 || fail "uv.lock の検査に必要な python3 がありません"
python3 - "$ROOT/uv.lock" "$version" <<'PY'
import sys
import tomllib


def fail(message: str) -> None:
    print(f"check-version: {message}", file=sys.stderr)
    raise SystemExit(1)


lock_path, expected_version = sys.argv[1:]
try:
    with open(lock_path, "rb") as lock_file:
        lock = tomllib.load(lock_file)
except (OSError, tomllib.TOMLDecodeError):
    fail("uv.lock を解析できません")

packages = lock.get("package")
if not isinstance(packages, list) or any(not isinstance(package, dict) for package in packages):
    fail("uv.lock の package 構造が不正です")

workspace_packages = {
    "narumi": "pipeline",
    "narumi-server": "server",
}
for name, editable_path in workspace_packages.items():
    matches = [package for package in packages if package.get("name") == name]
    if not matches:
        fail(f"uv.lock に workspace package '{name}' がありません")
    if len(matches) != 1:
        fail(f"uv.lock の workspace package '{name}' が重複しています")

    package = matches[0]
    source = package.get("source")
    if not isinstance(source, dict) or source.get("editable") != editable_path:
        fail(f"uv.lock の '{name}' が正しい workspace package ではありません")
    if package.get("version") != expected_version:
        fail(f"uv.lock {name} version != VERSION={expected_version}")
PY

pyproject_version() {
  # `version = "X.Y.Z"` の最初の 1 行（uv / hatchling 管理の固定フォーマット）
  sed -n 's/^version = "\(.*\)"$/\1/p' "$1" | head -n 1
}

pipeline_version="$(pyproject_version "$ROOT/pipeline/pyproject.toml")"
server_version="$(pyproject_version "$ROOT/server/pyproject.toml")"
[[ -n "$pipeline_version" ]] || fail "pipeline/pyproject.toml から version を読めません"
[[ -n "$server_version" ]] || fail "server/pyproject.toml から version を読めません"

package_version() {
  sed -n 's/^__version__ = "\(.*\)"$/\1/p' "$1" | head -n 1
}

pipeline_runtime_version="$(package_version "$ROOT/pipeline/src/narumi/__init__.py")"
server_runtime_version="$(package_version "$ROOT/server/src/narumi_server/__init__.py")"
recorder_version="$(sed -n 's/^    public static let version = "\(.*\)"$/\1/p' \
  "$ROOT/app/Sources/NarumiRecorderKit/RecorderEvents.swift" | head -n 1)"

[[ -f "$ROOT/CHANGELOG.md" ]] || fail "CHANGELOG.md がありません"
changelog_version="$(sed -n 's/^## \([0-9][^ ]*\).*/\1/p' "$ROOT/CHANGELOG.md" | head -n 1)"
[[ -n "$changelog_version" ]] || fail "CHANGELOG.md に '## <version>' 見出しがありません"

status=0
[[ "$pipeline_version" == "$version" ]] \
  || { echo "check-version: pipeline/pyproject.toml=$pipeline_version != VERSION=$version" >&2; status=1; }
[[ "$server_version" == "$version" ]] \
  || { echo "check-version: server/pyproject.toml=$server_version != VERSION=$version" >&2; status=1; }
[[ "$pipeline_runtime_version" == "$version" ]] \
  || { echo "check-version: pipeline __version__=$pipeline_runtime_version != VERSION=$version" >&2; status=1; }
[[ "$server_runtime_version" == "$version" ]] \
  || { echo "check-version: server __version__=$server_runtime_version != VERSION=$version" >&2; status=1; }
[[ "$recorder_version" == "$version" ]] \
  || { echo "check-version: recorder=$recorder_version != VERSION=$version" >&2; status=1; }
[[ "$changelog_version" == "$version" ]] \
  || { echo "check-version: CHANGELOG.md 最新見出し=$changelog_version != VERSION=$version" >&2; status=1; }
[[ $status -eq 0 ]] || exit 1

echo "check-version: OK ($version)"
