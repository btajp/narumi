#!/usr/bin/env bash
# scripts/release-app.sh — narumi.app の署名・公証・GitHub Releases 公開（設計 §3）。
#
# Usage: scripts/release-app.sh <version> [--allow-pubkey-rotation]
#
# 手順: 1 前提検査 → 2 版整合 → 3 鍵ポリシー → 4 ビルド（ランタイム同梱・Developer ID 署名）→
#       5 検証 → 6 公証 + staple → 7 appcast 生成・署名検証 → 8 GitHub Release（draft）
# タグ v<version> は gh release create が作る（先に手で打たない）。draft の公開は手動。
#
# Env（必須）:
#   APPLE_SIGNING_IDENTITY  "Developer ID Application: ..."
#   APPLE_API_KEY           App Store Connect API キー ID
#   APPLE_API_ISSUER        App Store Connect API issuer ID
#   APPLE_API_KEY_PATH      .p8 キーファイルのパス
# Env（任意）:
#   SPARKLE_VERSION         Sparkle ツールの版（既定 2.9.6）
#   SPARKLE_BIN             generate_appcast / sign_update / generate_keys のディレクトリ
#                           （既定 ~/.sparkle/<SPARKLE_VERSION>/bin）
#   RELEASE_DIR             作業ディレクトリ（既定 <repo>/dist/release/v<version>）
#
# 秘密情報（Apple API キー・Sparkle 秘密鍵）はリポ・ログに書かない。env と Keychain のみ。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SPARKLE_VERSION="${SPARKLE_VERSION:-2.9.6}"
SPARKLE_BIN="${SPARKLE_BIN:-$HOME/.sparkle/$SPARKLE_VERSION/bin}"
APP="$ROOT/dist/narumi.app"
PUBLIC_KEY_FILE="$ROOT/app/sparkle-public-key.txt"
DOWNLOAD_URL_PREFIX_BASE="https://github.com/btajp/narumi/releases/download"

fail() {
  echo "release-app: $*" >&2
  exit 1
}

step() {
  echo
  echo "===> $*"
}

usage() {
  sed -n '2,/^set -euo pipefail/p' "${BASH_SOURCE[0]}" | sed '$d' | sed 's/^# \{0,1\}//'
}

VERSION_ARG=""
ALLOW_ROTATION=0
for arg in "$@"; do
  case "$arg" in
    --allow-pubkey-rotation) ALLOW_ROTATION=1 ;;
    -h|--help) usage; exit 0 ;;
    -*) fail "unknown argument: ${arg}（--help 参照）" ;;
    *)
      [[ -z "$VERSION_ARG" ]] || fail "版は 1 つだけ指定してください"
      VERSION_ARG="$arg"
      ;;
  esac
done
[[ -n "$VERSION_ARG" ]] || { usage >&2; exit 2; }

# --- 1. 前提検査 ---------------------------------------------------------------------------
step "1/8 前提検査"

command -v gh >/dev/null 2>&1 || fail "gh がありません（brew install gh）"
command -v uv >/dev/null 2>&1 || fail "uv がありません（https://docs.astral.sh/uv/）"
command -v python3 >/dev/null 2>&1 || fail "python3 がありません"
command -v xcrun >/dev/null 2>&1 || fail "xcrun がありません（Xcode / CLT）"

gh auth status >/dev/null || fail "gh が未認証です（gh auth login）"

branch="$(git -C "$ROOT" rev-parse --abbrev-ref HEAD)"
[[ "$branch" == "main" ]] || fail "main ブランチでのみリリースできます（現在: ${branch}）"
[[ -z "$(git -C "$ROOT" status --porcelain)" ]] || fail "作業ツリーが clean ではありません"
git -C "$ROOT" fetch --quiet origin main
[[ "$(git -C "$ROOT" rev-parse HEAD)" == "$(git -C "$ROOT" rev-parse origin/main)" ]] \
  || fail "HEAD が origin/main と一致しません（push 済みの main からのみリリース）"

for tool in generate_appcast sign_update generate_keys; do
  if [[ ! -x "$SPARKLE_BIN/$tool" ]]; then
    fail "Sparkle ツールがありません: $SPARKLE_BIN/$tool
  取得方法:
    mkdir -p ~/.sparkle/$SPARKLE_VERSION
    gh release download --repo sparkle-project/Sparkle $SPARKLE_VERSION \\
      --pattern 'Sparkle-$SPARKLE_VERSION.tar.xz' --output ~/.sparkle/Sparkle-$SPARKLE_VERSION.tar.xz
    tar xf ~/.sparkle/Sparkle-$SPARKLE_VERSION.tar.xz -C ~/.sparkle/$SPARKLE_VERSION
  （bin/ が ~/.sparkle/$SPARKLE_VERSION/bin に展開される。別の場所なら SPARKLE_BIN で指定）"
  fi
done

[[ -n "${APPLE_SIGNING_IDENTITY:-}" ]] || fail "APPLE_SIGNING_IDENTITY が未設定です"
[[ -n "${APPLE_API_KEY:-}" ]] || fail "APPLE_API_KEY が未設定です"
[[ -n "${APPLE_API_ISSUER:-}" ]] || fail "APPLE_API_ISSUER が未設定です"
[[ -n "${APPLE_API_KEY_PATH:-}" ]] || fail "APPLE_API_KEY_PATH が未設定です"
[[ -f "$APPLE_API_KEY_PATH" ]] || fail "APPLE_API_KEY_PATH のファイルがありません"

"$SPARKLE_BIN/generate_keys" -p >/dev/null \
  || fail "Keychain に Sparkle 秘密鍵がありません。初回はリリース担当者が generate_keys を一度だけ実行し、
  公開鍵を app/sparkle-public-key.txt にコミット、generate_keys -x で秘密鍵をバックアップする（README「配布」参照）"

# --- 2. 版整合 -----------------------------------------------------------------------------
step "2/8 版整合（VERSION が正本）"
version_file="$(tr -d '[:space:]' < "$ROOT/VERSION")"
[[ "$VERSION_ARG" == "$version_file" ]] \
  || fail "指定の版 $VERSION_ARG が VERSION ファイル（${version_file}）と一致しません"
"$ROOT/scripts/check-version.sh"
VERSION="$version_file"
RELEASE_DIR="${RELEASE_DIR:-$ROOT/dist/release/v$VERSION}"
FEED_DIR="$RELEASE_DIR/feed"
ZIP="$FEED_DIR/narumi-$VERSION.zip"

if (cd "$ROOT" && gh release view "v$VERSION" >/dev/null 2>&1); then
  fail "リリース v$VERSION は既に存在します"
fi

# --- 3. 鍵ポリシー -------------------------------------------------------------------------
step "3/8 鍵ポリシー"
rotation_args=()
if [[ $ALLOW_ROTATION -eq 1 ]]; then
  rotation_args+=(--allow-pubkey-rotation)
fi
SPARKLE_BIN="$SPARKLE_BIN" "$ROOT/scripts/check-updater-key-policy.sh" ${rotation_args[@]+"${rotation_args[@]}"}

# --- 4. ビルド -----------------------------------------------------------------------------
step "4/8 ビルド（swift build + ランタイム同梱 + Developer ID 署名）"
"$ROOT/scripts/build-app.sh" --release --runtime

# --- 5. 検証 -------------------------------------------------------------------------------
step "5/8 検証"
codesign --verify --deep --strict "$APP"
# spctl は公証前は rejected が正常。ここでは参考表示のみ、staple 後に必須検査する。
spctl -a -t exec -vv "$APP" || echo "release-app: spctl は公証前のため rejected（staple 後に再検査）"

plist_version="$(/usr/libexec/PlistBuddy -c 'Print CFBundleShortVersionString' "$APP/Contents/Info.plist")"
[[ "$plist_version" == "$VERSION" ]] \
  || fail "Info.plist の CFBundleShortVersionString（${plist_version}）が $VERSION と一致しません"
plist_pubkey="$(/usr/libexec/PlistBuddy -c 'Print SUPublicEDKey' "$APP/Contents/Info.plist")" \
  || fail "Info.plist に SUPublicEDKey がありません"
committed_pubkey="$(tr -d '[:space:]' < "$PUBLIC_KEY_FILE")"
[[ "$plist_pubkey" == "$committed_pubkey" ]] \
  || fail "Info.plist の SUPublicEDKey が app/sparkle-public-key.txt と一致しません"

# --- 6. 公証 + staple ----------------------------------------------------------------------
step "6/8 公証（notarytool submit --wait → stapler staple → zip 再作成）"
mkdir -p "$FEED_DIR"
rm -f "$ZIP"
ditto -c -k --sequesterRsrc --keepParent "$APP" "$ZIP"
xcrun notarytool submit "$ZIP" \
  --key "$APPLE_API_KEY_PATH" --key-id "$APPLE_API_KEY" --issuer "$APPLE_API_ISSUER" \
  --wait
xcrun stapler staple "$APP"
rm -f "$ZIP"
ditto -c -k --sequesterRsrc --keepParent "$APP" "$ZIP"
spctl -a -t exec -vv "$APP" || fail "spctl の評価に失敗しました（公証後）"

# --- 7. appcast ----------------------------------------------------------------------------
step "7/8 appcast 生成（minimumSystemVersion / hardwareRequirements 付与、署名検証）"
"$SPARKLE_BIN/generate_appcast" \
  --download-url-prefix "$DOWNLOAD_URL_PREFIX_BASE/v$VERSION/" \
  -o "$FEED_DIR/appcast.xml" \
  "$FEED_DIR"

# generate_appcast はバンドルから最小 OS / アーキテクチャを推定するが、欠けていた場合に備えて
# sparkle:minimumSystemVersion 15.0 / sparkle:hardwareRequirements arm64 を必ず付与する（冪等）。
# 対象は enclosure の EdDSA 署名であって appcast.xml 自体ではないため、この編集で署名は壊れない。
python3 - "$FEED_DIR/appcast.xml" <<'PY'
import sys
import xml.etree.ElementTree as ET

NS = "http://www.andymatuschak.org/xml-namespaces/sparkle"
ET.register_namespace("sparkle", NS)
tree = ET.parse(sys.argv[1])
changed = False
for item in tree.getroot().iter("item"):
    for tag, value in ((f"{{{NS}}}minimumSystemVersion", "15.0"),
                       (f"{{{NS}}}hardwareRequirements", "arm64")):
        if item.find(tag) is None:
            ET.SubElement(item, tag).text = value
            changed = True
tree.write(sys.argv[1], encoding="UTF-8", xml_declaration=True)
print("appcast.xml:", "requirements injected" if changed else "requirements already present")
PY

# enclosure（zip / delta）の sparkle:edSignature を検証する。sign_update --verify は Keychain の
# 秘密鍵から導出した公開鍵で検証する — 手順 3 で app/sparkle-public-key.txt との整合を確認済み
# （ローテーション時は旧鍵での検証 = 既存ユーザーの検証経路そのもの）。
python3 - "$FEED_DIR/appcast.xml" <<'PY' > "$RELEASE_DIR/enclosures.tsv"
import sys
import urllib.parse
import xml.etree.ElementTree as ET

NS = "http://www.andymatuschak.org/xml-namespaces/sparkle"
tree = ET.parse(sys.argv[1])
count = 0
for enclosure in tree.getroot().iter("enclosure"):
    url = enclosure.get("url")
    sig = enclosure.get(f"{{{NS}}}edSignature")
    if not url or not sig:
        sys.exit(f"enclosure に url / sparkle:edSignature がありません: {url}")
    name = urllib.parse.unquote(url.rsplit("/", 1)[-1])
    print(f"{name}\t{sig}")
    count += 1
if count == 0:
    sys.exit("appcast.xml に enclosure がありません")
PY
while IFS=$'\t' read -r asset signature; do
  [[ -f "$FEED_DIR/$asset" ]] || fail "appcast が参照する $asset が $FEED_DIR にありません"
  "$SPARKLE_BIN/sign_update" --verify "$FEED_DIR/$asset" "$signature" \
    || fail "$asset の sparkle:edSignature を検証できません"
  echo "release-app: edSignature OK: $asset"
done < "$RELEASE_DIR/enclosures.tsv"

# --- 8. 公開（draft） ----------------------------------------------------------------------
step "8/8 GitHub Release（draft 作成。タグはここで作られる）"
notes_file="$RELEASE_DIR/notes.md"
awk -v ver="$VERSION" '
  /^## / { emit = ($2 == ver); next }
  emit { print }
' "$ROOT/CHANGELOG.md" > "$notes_file"
[[ -s "$notes_file" ]] || fail "CHANGELOG.md に ## $VERSION のセクションがありません"

assets=("$ZIP" "$FEED_DIR/appcast.xml")
shopt -s nullglob
assets+=("$FEED_DIR"/*.delta)
shopt -u nullglob

(cd "$ROOT" && gh release create "v$VERSION" \
  --draft \
  --target "$(git rev-parse HEAD)" \
  --title "v$VERSION" \
  --notes-file "$notes_file" \
  "${assets[@]}")

echo
echo "release-app: draft v$VERSION を作成しました。内容を確認してから publish してください:"
echo "  gh release view v$VERSION --web"
echo "  （appcast.xml の SUFeedURL は releases/latest/download を指すため、publish 後に有効になる）"
