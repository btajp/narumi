#!/bin/bash
# Build, notarize, validate, and upload a draft. This script never publishes a release.
# Usage: scripts/release-app.sh <version> [--verify-draft|--verify-published]
# Required env: APPLE_SIGNING_IDENTITY, APPLE_API_KEY, APPLE_API_ISSUER, APPLE_API_KEY_PATH.
# Optional env:
#   NARUMI_RELEASE_ENV  Local shell settings (default ~/.config/narumi/release.env if present).
#   SPARKLE_BIN        Existing tools; otherwise use app/.build/artifacts/*/*/bin.
#   SPARKLE_KEY_ACCOUNT  Existing signing account (default jp.btajp.narumi).
#   SPARKLE_CRITICAL_UPDATE  Set to 1 only when every older installed version must present
#                            this update immediately (default 0).
#   RELEASE_DIR        Unused absolute directory (default <repo>/dist/release/v<version>).
# --verify-* only checks an existing release; it never overwrites or publishes anything.
set +x
set +v
set -euo pipefail
umask 077

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$ROOT/scripts/release-common.sh"

fail() {
  echo "release-app: $*" >&2
  exit 1
}

step() {
  echo "release-app: $*"
}

usage() {
  echo "Usage: scripts/release-app.sh <version> [--verify-draft|--verify-published]"
}

notarize() {
  local artifact="$1" result="$2" diagnostic="$3"
  if ! xcrun notarytool submit "$artifact" \
    --key "$APPLE_API_KEY_PATH" --key-id "$APPLE_API_KEY" --issuer "$APPLE_API_ISSUER" \
    --wait --output-format json > "$result" 2> "$diagnostic"; then
    fail "公証に失敗しました。出荷は中止し、専用ディレクトリに診断を保存しました"
  fi
}

VERSION=""
MODE="create"
for arg in "$@"; do
  case "$arg" in
    --verify-draft|--verify-published)
      [[ "$MODE" == "create" ]] || fail "検証モードは 1 つだけ指定してください"
      MODE="${arg#--}"
      ;;
    --allow-pubkey-rotation) fail "鍵不一致での出荷は許可しません" ;;
    -h|--help) usage; exit 0 ;;
    -*) fail "unknown argument（--help 参照）" ;;
    *) [[ -z "$VERSION" ]] || fail "版は 1 つだけ指定してください"; VERSION="$arg" ;;
  esac
done
[[ -n "$VERSION" ]] || { usage >&2; exit 2; }
[[ "$VERSION" =~ ^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$ ]] \
  || fail "安定版 semver（X.Y.Z、先頭ゼロなし）を指定してください"

release_load_env || fail "リリース設定を読み込めません"
SPARKLE_CRITICAL_UPDATE="${SPARKLE_CRITICAL_UPDATE:-0}"
case "$SPARKLE_CRITICAL_UPDATE" in
  0|1) ;;
  *) fail "SPARKLE_CRITICAL_UPDATE は 0 または 1 で指定してください" ;;
esac
SPARKLE_BIN="$(release_sparkle_bin "$ROOT")" \
  || fail "Sparkle ツールがありません。SwiftPM の依存解決後、または SPARKLE_BIN 指定で再実行してください"
SPARKLE_KEY_ACCOUNT="${SPARKLE_KEY_ACCOUNT:-jp.btajp.narumi}"
RELEASE_DIR="${RELEASE_DIR:-$ROOT/dist/release/v$VERSION}"
PUBLIC_KEY_FILE="$ROOT/app/sparkle-public-key.txt"
BUILD_DIR="$RELEASE_DIR/build"
APP="$BUILD_DIR/narumi.app"
FEED_DIR="$RELEASE_DIR/feed"
ZIP="$FEED_DIR/narumi-$VERSION.zip"
INSTALLER_DIR="$RELEASE_DIR/installer"
DMG="$INSTALLER_DIR/narumi-$VERSION.dmg"
DMG_HELPER="$ROOT/scripts/release_dmg.py"
VERIFY="$ROOT/scripts/release_verify.py"
INVENTORY="$ROOT/scripts/bundle_inventory.py"
REPO="btajp/narumi"
export SPARKLE_BIN SPARKLE_KEY_ACCOUNT

for tool in gh git python3; do
  command -v "$tool" >/dev/null 2>&1 || fail "$tool がありません。既存のツール管理方針に沿って準備してください"
done
gh auth status >/dev/null 2>&1 || fail "gh の認証を確認できません"
"$ROOT/scripts/check-version.sh"
"$ROOT/scripts/check-updater-key-policy.sh"
verify_args=(--root "$ROOT" --release-dir "$RELEASE_DIR" --version "$VERSION"
  --sparkle-bin "$SPARKLE_BIN" --account "$SPARKLE_KEY_ACCOUNT")

if [[ "$MODE" != "create" ]]; then
  python3 "$VERIFY" "$MODE" "${verify_args[@]}"
  exit 0
fi

step "1/9 出荷元と既存リリースを確認"
for tool in uv xcrun codesign spctl ditto; do
  command -v "$tool" >/dev/null 2>&1 || fail "$tool がありません"
done
[[ "${APPLE_SIGNING_IDENTITY:-}" == "Developer ID Application: "* ]] \
  || fail "Developer ID Application の署名 identity が必要です"
[[ -n "${APPLE_API_KEY:-}" && -n "${APPLE_API_ISSUER:-}" && -n "${APPLE_API_KEY_PATH:-}" ]] \
  || fail "Apple 公証用の設定が不足しています"
[[ -f "$APPLE_API_KEY_PATH" ]] || fail "Apple 公証用キーファイルがありません"
source_info="$(python3 "$VERIFY" preflight "${verify_args[@]}")"
IFS=$'\t' read -r SOURCE_COMMIT BUILD_NUMBER <<< "$source_info"
RELEASE_SCHEMA="$(python3 -c \
  'import json, sys; print(json.load(open(sys.argv[1]))["schema_version"])' \
  "$RELEASE_DIR/source.json")"
if [[ "$RELEASE_SCHEMA" == "2" ]]; then
  command -v hdiutil >/dev/null 2>&1 || fail "hdiutil がありません"
  [[ -f "$DMG_HELPER" ]] || fail "DMG helper がありません"
fi

step "2/9 専用ディレクトリへビルド・署名"
# Do not inherit a caller's DIST_DIR or public-key override. The live dist app is untouched.
DIST_DIR="$BUILD_DIR" SPARKLE_PUBLIC_KEY_FILE="$PUBLIC_KEY_FILE" \
  NARUMI_TRACKED_SOURCES="$RELEASE_DIR/tracked-sources.nul" \
  "$ROOT/scripts/build-app.sh" --release --runtime --build-override "$BUILD_NUMBER"
python3 "$VERIFY" check-app "${verify_args[@]}"
python3 "$INVENTORY" check-app "$APP" --require-runtime \
  --tracked-sources "$RELEASE_DIR/tracked-sources.nul" > "$RELEASE_DIR/pre-notary-inventory.json"
codesign --verify --deep --strict "$APP"

step "3/9 Apple 公証"
mkdir "$FEED_DIR"
NOTARY_ZIP="$RELEASE_DIR/notary-submission.zip"
ditto -c -k --norsrc --noextattr --noqtn --keepParent "$APP" "$NOTARY_ZIP"
notarize "$NOTARY_ZIP" "$RELEASE_DIR/notary-result.json" "$RELEASE_DIR/notary-error.log"
python3 "$VERIFY" check-notary "${verify_args[@]}"

step "4/9 staple 後の最終 ZIP を検査"
xcrun stapler staple "$APP"
xcrun stapler validate "$APP"
codesign --verify --deep --strict "$APP"
spctl -a -t exec -vv "$APP"
ditto -c -k --norsrc --noextattr --noqtn --keepParent "$APP" "$ZIP"
python3 "$INVENTORY" check-zip "$ZIP" --require-runtime \
  --tracked-sources "$RELEASE_DIR/tracked-sources.nul" > "$RELEASE_DIR/final-zip-inventory.json"
# Verify that ZIP creation preserved the stapled ticket and signatures.
UNPACKED_DIR="$RELEASE_DIR/verify-unpacked"
mkdir "$UNPACKED_DIR"
ditto -x -k "$ZIP" "$UNPACKED_DIR"
xcrun stapler validate "$UNPACKED_DIR/narumi.app"
codesign --verify --deep --strict "$UNPACKED_DIR/narumi.app"
spctl -a -t exec -vv "$UNPACKED_DIR/narumi.app"

step "5/9 初回導入用 DMG を作成・署名・公証"
if [[ "$RELEASE_SCHEMA" == "2" ]]; then
  mkdir "$INSTALLER_DIR"
  if ! python3 "$DMG_HELPER" create --zip "$ZIP" --output "$DMG" \
    --work-dir "$RELEASE_DIR" --tracked-sources "$RELEASE_DIR/tracked-sources.nul" \
    > "$RELEASE_DIR/installer-create.json" 2> "$RELEASE_DIR/installer-create-error.log"; then
    fail "DMG 作成に失敗しました。診断は専用ディレクトリに保存しました"
  fi
  codesign --sign "$APPLE_SIGNING_IDENTITY" --timestamp --identifier jp.btajp.narumi.dmg "$DMG"
  notarize "$DMG" "$RELEASE_DIR/installer-notary-result.json" \
    "$RELEASE_DIR/installer-notary-error.log"
  python3 "$VERIFY" check-installer-notary "${verify_args[@]}"
  xcrun stapler staple "$DMG"
  xcrun stapler validate "$DMG"
  codesign --verify --strict "$DMG"
else
  step "旧版 schema のため DMG は追加しません"
fi

step "6/9 ZIP の EdDSA 署名と appcast を生成"
"$SPARKLE_BIN/sign_update" --account "$SPARKLE_KEY_ACCOUNT" -p "$ZIP" \
  > "$RELEASE_DIR/archive-signature.txt"
appcast_args=(--maximum-versions 1 --maximum-deltas 0)
if [[ "$SPARKLE_CRITICAL_UPDATE" == "1" ]]; then
  # Empty version means the update is critical for every older Sparkle 2 host.
  appcast_args+=(--critical-update-version "")
fi
"$SPARKLE_BIN/generate_appcast" --account "$SPARKLE_KEY_ACCOUNT" \
  "${appcast_args[@]}" \
  --download-url-prefix "https://github.com/$REPO/releases/download/v$VERSION/" \
  -o "$FEED_DIR/appcast.xml" "$FEED_DIR"

step "7/9 版・署名・公開鍵・SHA256 を封印"
python3 "$VERIFY" seal "${verify_args[@]}"
notes_file="$RELEASE_DIR/notes.md"
awk -v ver="$VERSION" '
  /^## / { emit = ($2 == ver); next }
  emit { print }
' "$ROOT/CHANGELOG.md" > "$notes_file"
[[ -s "$notes_file" ]] || fail "CHANGELOG.md に指定版のセクションがありません"

step "8/9 出荷直前の再照合と draft 作成"
python3 "$VERIFY" verify-ready "${verify_args[@]}"
# The target is the captured full SHA, never a moving branch. No globs or delta assets.
publish_assets=("$ZIP" "$FEED_DIR/appcast.xml")
if [[ "$RELEASE_SCHEMA" == "2" ]]; then
  publish_assets+=("$DMG")
fi
gh release create "v$VERSION" --repo "$REPO" --draft --target "$SOURCE_COMMIT" \
  --title "v$VERSION" --notes-file "$notes_file" "${publish_assets[@]}"

step "9/9 draft を再取得して照合"
python3 "$VERIFY" verify-draft "${verify_args[@]}"
echo "release-app: v$VERSION の draft 準備が完了しました。まだ公開していません。"
echo "同じ RELEASE_DIR の --verify-draft 成功後、同じ release ID / tag / commit のまま公開できます。"
echo "公開後に --verify-published で匿名配布を照合し、DMG / Sparkle 経由で実環境を確認します。"
