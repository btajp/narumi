#!/usr/bin/env bash
# scripts/check-version.sh — VERSION ファイルを正本に、版の整合を検査する。
#
# 検査対象（すべて一致すること）:
#   VERSION / pipeline/pyproject.toml / server/pyproject.toml / CHANGELOG.md の最新見出し
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

pyproject_version() {
  # `version = "X.Y.Z"` の最初の 1 行（uv / hatchling 管理の固定フォーマット）
  sed -n 's/^version = "\(.*\)"$/\1/p' "$1" | head -n 1
}

pipeline_version="$(pyproject_version "$ROOT/pipeline/pyproject.toml")"
server_version="$(pyproject_version "$ROOT/server/pyproject.toml")"
[[ -n "$pipeline_version" ]] || fail "pipeline/pyproject.toml から version を読めません"
[[ -n "$server_version" ]] || fail "server/pyproject.toml から version を読めません"

[[ -f "$ROOT/CHANGELOG.md" ]] || fail "CHANGELOG.md がありません"
changelog_version="$(sed -n 's/^## \([0-9][^ ]*\).*/\1/p' "$ROOT/CHANGELOG.md" | head -n 1)"
[[ -n "$changelog_version" ]] || fail "CHANGELOG.md に '## <version>' 見出しがありません"

status=0
[[ "$pipeline_version" == "$version" ]] \
  || { echo "check-version: pipeline/pyproject.toml=$pipeline_version != VERSION=$version" >&2; status=1; }
[[ "$server_version" == "$version" ]] \
  || { echo "check-version: server/pyproject.toml=$server_version != VERSION=$version" >&2; status=1; }
[[ "$changelog_version" == "$version" ]] \
  || { echo "check-version: CHANGELOG.md 最新見出し=$changelog_version != VERSION=$version" >&2; status=1; }
[[ $status -eq 0 ]] || exit 1

echo "check-version: OK ($version)"
