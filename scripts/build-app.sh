#!/bin/bash
# Use the macOS system shell independently of the user's package-manager PATH.
# Build the Swift package in release mode and assemble dist/narumi.app
# (menu bar UI + recorder / Keychain helpers + Sparkle.framework, optionally Python).
#
# Usage: scripts/build-app.sh [options]
#   --skip-build            app/.build/release の既存バイナリを再利用する
#   --release               リリースビルド: ${APPLE_SIGNING_IDENTITY}（Developer ID）必須、
#                           hardened runtime + timestamp で署名、Sparkle.framework と
#                           SUPublicEDKey が無ければエラー
#   --runtime               Contents/Resources/runtime/ を組み立てる（uv バイナリ・wheels・
#                           requirements.txt・contracts・manifest.json — 自己完結 .app 用）
#   --version-override <v>  CFBundleShortVersionString / manifest の app_version を上書き
#                           （既定: VERSION ファイル。更新 E2E 用）
#   --build-override <n>    CFBundleVersion を上書き（既定: git rev-list --count HEAD。
#                           Sparkle は CFBundleVersion で新旧を比較するため、E2E では
#                           旧版より大きい値を渡す）
#
# Env:
#   DIST_DIR                 出力先（既定 <repo>/dist）
#   APPLE_SIGNING_IDENTITY   codesign identity（既定: ad-hoc "-"）
#   SPARKLE_PUBLIC_KEY_FILE  SUPublicEDKey の読み出し元（既定 <repo>/app/sparkle-public-key.txt。
#                            無ければ警告してキーを埋め込まない。--release では必須）
#   NARUMI_UV_CACHE_DIR      uv 配布物のダウンロードキャッシュ
#                            （既定 ~/Library/Caches/narumi-build/uv。sha256 は毎回検証）
#   NARUMI_TRACKED_SOURCES   wheel と照合する Git 管理下の source path（NUL 区切り）。
#                            release-app.sh が作成する。指定時は未管理ファイル混入を拒否
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_DIR="$ROOT/app"
DIST_DIR="${DIST_DIR:-$ROOT/dist}"
APP_NAME="narumi"
BUNDLE_ID="jp.btajp.narumi"
BUNDLE="$DIST_DIR/$APP_NAME.app"
RUNTIME_LOCK="$ROOT/scripts/runtime.lock.json"
APP_ICON="$APP_DIR/Assets/AppIcon.icns"
RECORDING_ENTITLEMENTS="$APP_DIR/recording.entitlements.plist"
INVENTORY="$ROOT/scripts/bundle_inventory.py"
PUBLIC_KEY_FILE="${SPARKLE_PUBLIC_KEY_FILE:-$APP_DIR/sparkle-public-key.txt}"
UV_CACHE_DIR="${NARUMI_UV_CACHE_DIR:-$HOME/Library/Caches/narumi-build/uv}"
FEED_URL="https://github.com/btajp/narumi/releases/latest/download/appcast.xml"

SKIP_BUILD=0
RELEASE=0
WITH_RUNTIME=0
VERSION_OVERRIDE=""
BUILD_OVERRIDE=""

usage() {
  # ヘッダコメント（`set -euo pipefail` の手前まで）を表示する
  sed -n '2,/^set -euo pipefail/p' "${BASH_SOURCE[0]}" | sed '$d' | sed 's/^# \{0,1\}//'
}

fail() {
  echo "build-app: $*" >&2
  exit 1
}

warn() {
  echo "build-app: WARNING: $*" >&2
}

validate_recording_entitlements() {
  python3 -c '
import plistlib
import sys

try:
    entitlements = plistlib.loads(sys.stdin.buffer.read())
except Exception:
    sys.exit(1)
key = "com.apple.security.device.audio-input"
if not isinstance(entitlements, dict) or set(entitlements) != {key} or entitlements[key] is not True:
    sys.exit(1)
'
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --skip-build) SKIP_BUILD=1 ;;
    --release) RELEASE=1 ;;
    --runtime) WITH_RUNTIME=1 ;;
    --version-override)
      [[ $# -ge 2 ]] || fail "--version-override には値が必要です"
      VERSION_OVERRIDE="$2"
      shift
      ;;
    --build-override)
      [[ $# -ge 2 ]] || fail "--build-override には値が必要です"
      BUILD_OVERRIDE="$2"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *) fail "unknown argument: $1（--help 参照）" ;;
  esac
  shift
done

# --- asset and packaging preflight ----------------------------------------------------------
# 既存 bundle を変更する前に、必須アセットと検査プログラムを検証する。
[[ -f "$APP_ICON" && ! -L "$APP_ICON" ]] || fail "app icon がありません: $APP_ICON"
ICON_HEADER="$(od -An -v -tx1 -N8 "$APP_ICON" | tr -d '[:space:]')"
[[ "${#ICON_HEADER}" -eq 16 && "$ICON_HEADER" == 69636e73* ]] \
  || fail "AppIcon.icns のヘッダーが不正です"
ICON_SIZE="$(wc -c < "$APP_ICON" | tr -d '[:space:]')"
ICON_DECLARED_SIZE=$((16#${ICON_HEADER:8:8}))
[[ "$ICON_SIZE" -gt 8 && "$ICON_SIZE" -eq "$ICON_DECLARED_SIZE" ]] \
  || fail "AppIcon.icns のサイズがヘッダーと一致しません"
command -v python3 >/dev/null 2>&1 || fail "python3 が必要です（bundle inventory）"
[[ -f "$RECORDING_ENTITLEMENTS" && ! -L "$RECORDING_ENTITLEMENTS" ]] \
  || fail "録音用 entitlement がありません: $RECORDING_ENTITLEMENTS"
validate_recording_entitlements < "$RECORDING_ENTITLEMENTS" \
  || fail "録音用 entitlement は audio-input=true のみを含む plist が必要です"
[[ -f "$INVENTORY" ]] || fail "bundle inventory helper がありません"
python3 "$INVENTORY" --help >/dev/null || fail "bundle inventory helper を起動できません"
if [[ -n "${NARUMI_TRACKED_SOURCES:-}" ]]; then
  [[ -f "$NARUMI_TRACKED_SOURCES" ]] || fail "NARUMI_TRACKED_SOURCES のファイルがありません"
fi

# --- versions ------------------------------------------------------------------------------
[[ -f "$ROOT/VERSION" ]] || fail "VERSION ファイルがありません"
VERSION="$(tr -d '[:space:]' < "$ROOT/VERSION")"
if [[ -n "$VERSION_OVERRIDE" ]]; then
  VERSION="$VERSION_OVERRIDE"
fi
[[ -n "$VERSION" ]] || fail "版が空です"

if [[ -n "$BUILD_OVERRIDE" ]]; then
  [[ "$BUILD_OVERRIDE" =~ ^[0-9]+$ ]] || fail "--build-override は整数で指定してください"
  BUILD_NUMBER="$BUILD_OVERRIDE"
else
  BUILD_NUMBER="$(git -C "$ROOT" rev-list --count HEAD)" \
    || fail "git rev-list --count HEAD に失敗（git リポジトリ外？ --build-override を使ってください）"
fi

# --- signing setup -------------------------------------------------------------------------
IDENTITY="${APPLE_SIGNING_IDENTITY:--}"
SIGN_FLAGS=(--force --sign "$IDENTITY")
if [[ $RELEASE -eq 1 ]]; then
  [[ "$IDENTITY" == "Developer ID Application: "* ]] \
    || fail "--release には APPLE_SIGNING_IDENTITY（Developer ID Application）が必要です"
  SIGN_FLAGS+=(--options runtime --timestamp)
fi

# --- SUPublicEDKey -------------------------------------------------------------------------
PUBLIC_KEY=""
if [[ -f "$PUBLIC_KEY_FILE" ]]; then
  PUBLIC_KEY="$(tr -d '[:space:]' < "$PUBLIC_KEY_FILE")"
fi
if [[ -z "$PUBLIC_KEY" ]]; then
  if [[ $RELEASE -eq 1 ]]; then
    fail "SUPublicEDKey がありません: ${PUBLIC_KEY_FILE}（初回リリース手順は README の「配布」参照）"
  fi
  warn "SUPublicEDKey を埋め込みません（$PUBLIC_KEY_FILE が無いため。更新の検証は不可）"
fi

# --- swift build ---------------------------------------------------------------------------
if [[ $SKIP_BUILD -eq 0 ]]; then
  echo "==> swift build -c release"
  (cd "$APP_DIR" && swift build -c release)
fi

BIN_DIR="$APP_DIR/.build/release"
for bin in NarumiMenuBar narumi-recorder narumi-keychain; do
  [[ -x "$BIN_DIR/$bin" ]] \
    || fail "missing binary: $BIN_DIR/$bin (run without --skip-build)"
done

echo "==> assembling $BUNDLE (version $VERSION, build $BUILD_NUMBER)"
rm -rf "$BUNDLE"
mkdir -p "$BUNDLE/Contents/MacOS" "$BUNDLE/Contents/Resources"
cp "$BIN_DIR/NarumiMenuBar" "$BUNDLE/Contents/MacOS/NarumiMenuBar"
cp "$BIN_DIR/narumi-recorder" "$BUNDLE/Contents/MacOS/narumi-recorder"
cp "$BIN_DIR/narumi-keychain" "$BUNDLE/Contents/MacOS/narumi-keychain"
cp "$APP_ICON" "$BUNDLE/Contents/Resources/AppIcon.icns"
chmod 755 "$BUNDLE/Contents/MacOS/NarumiMenuBar" "$BUNDLE/Contents/MacOS/narumi-recorder" \
  "$BUNDLE/Contents/MacOS/narumi-keychain"

# --- Sparkle.framework ---------------------------------------------------------------------
# SwiftPM の binaryTarget が展開する xcframework から macOS スライスをコピーする
# （例: app/.build/artifacts/sparkle/Sparkle/Sparkle.xcframework/macos-arm64_x86_64/Sparkle.framework）。
SPARKLE_SRC=""
if [[ -d "$APP_DIR/.build/artifacts" ]]; then
  SPARKLE_SRC="$(find "$APP_DIR/.build/artifacts" -type d -name Sparkle.framework -path '*macos*' | head -n 1)"
fi
SPARKLE_DST="$BUNDLE/Contents/Frameworks/Sparkle.framework"
if [[ -n "$SPARKLE_SRC" ]]; then
  echo "==> embedding Sparkle.framework ($SPARKLE_SRC)"
  mkdir -p "$BUNDLE/Contents/Frameworks"
  # シンボリックリンク構造（Versions/Current など）を保ったままコピーする
  ditto "$SPARKLE_SRC" "$SPARKLE_DST"
  # サンドボックス非対応のため XPCServices は削除（Sparkle 公式 "Removing XPC Services"）。
  # ルートの XPCServices シンボリックリンクも道連れに消す（dangling symlink を残さない）。
  rm -rf "$SPARKLE_DST/Versions/B/XPCServices"
  rm -f "$SPARKLE_DST/XPCServices"
else
  if [[ $RELEASE -eq 1 ]]; then
    fail "Sparkle.framework が app/.build/artifacts に見つかりません（Package.swift の Sparkle 依存と swift build が必要）"
  fi
  warn "Sparkle.framework が見つからないため同梱しません（Package.swift の Sparkle 依存が未導入か）"
fi

# --- bundled runtime (--runtime) -----------------------------------------------------------
if [[ $WITH_RUNTIME -eq 1 ]]; then
  command -v python3 >/dev/null 2>&1 || fail "python3 が必要です（--runtime）"
  command -v uv >/dev/null 2>&1 || fail "uv が必要です（--runtime）"
  RUNTIME_DIR="$BUNDLE/Contents/Resources/runtime"
  mkdir -p "$RUNTIME_DIR"

  [[ -f "$RUNTIME_LOCK" ]] || fail "runtime.lock.json がありません: $RUNTIME_LOCK"
  UV_VERSION="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["uv"]["version"])' "$RUNTIME_LOCK")"
  UV_ARTIFACT="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["uv"]["artifact"])' "$RUNTIME_LOCK")"
  UV_URL="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["uv"]["url"])' "$RUNTIME_LOCK")"
  UV_SHA256="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["uv"]["sha256"])' "$RUNTIME_LOCK")"

  # uv 単体バイナリ: キャッシュ → sha256 検証 →（無ければ）ダウンロード → 再検証 → 展開
  UV_CACHED="$UV_CACHE_DIR/$UV_VERSION/$UV_ARTIFACT"
  verify_uv_tarball() {
    echo "$UV_SHA256  $1" | shasum -a 256 --check --status
  }
  if [[ -f "$UV_CACHED" ]] && verify_uv_tarball "$UV_CACHED"; then
    echo "==> uv $UV_VERSION (cached: $UV_CACHED)"
  else
    echo "==> downloading uv $UV_VERSION"
    mkdir -p "$(dirname "$UV_CACHED")"
    curl -fsSL --retry 3 --max-time 600 -o "$UV_CACHED.tmp" "$UV_URL"
    verify_uv_tarball "$UV_CACHED.tmp" \
      || { rm -f "$UV_CACHED.tmp"; fail "uv 配布物の sha256 が runtime.lock.json と一致しません"; }
    mv "$UV_CACHED.tmp" "$UV_CACHED"
  fi
  UV_EXTRACT="$(mktemp -d)"
  tar -xzf "$UV_CACHED" -C "$UV_EXTRACT"
  [[ -x "$UV_EXTRACT/uv-aarch64-apple-darwin/uv" ]] || fail "uv 配布物に uv バイナリがありません"
  cp "$UV_EXTRACT/uv-aarch64-apple-darwin/uv" "$RUNTIME_DIR/uv"
  chmod 755 "$RUNTIME_DIR/uv"
  rm -rf "$UV_EXTRACT"

  # wheels（narumi / narumi-server 本体）
  echo "==> uv build (wheels)"
  (
    WHEELS_TMP="$(mktemp -d)"
    trap 'rm -rf "$WHEELS_TMP"' EXIT
    # uv が生成する .gitignore などの sidecar は bundle へ持ち込まない。
    uv build --project "$ROOT" --package narumi --wheel --out-dir "$WHEELS_TMP"
    uv build --project "$ROOT" --package narumi-server --wheel --out-dir "$WHEELS_TMP"
    COPY_WHEELS_ARGS=(copy-wheels "$WHEELS_TMP" "$RUNTIME_DIR/wheels")
    if [[ -n "${NARUMI_TRACKED_SOURCES:-}" ]]; then
      COPY_WHEELS_ARGS+=(--tracked-sources "$NARUMI_TRACKED_SOURCES")
    fi
    python3 "$INVENTORY" "${COPY_WHEELS_ARGS[@]}" >/dev/null
  )

  # requirements.txt（サードパーティ依存の完全固定。2 つの export を結合・重複排除、ハッシュ維持）
  echo "==> uv export (requirements.txt)"
  EXPORT_TMP="$(mktemp -d)"
  (cd "$ROOT" && uv export --frozen --no-dev --no-emit-workspace --format requirements-txt \
    --package narumi-server --extra secure > "$EXPORT_TMP/server.txt")
  (cd "$ROOT" && uv export --frozen --no-dev --no-emit-workspace --format requirements-txt \
    --package narumi --extra whisper-mlx --extra claude --extra anthropic --extra html --extra slides \
    > "$EXPORT_TMP/pipeline.txt")
  python3 - "$EXPORT_TMP/server.txt" "$EXPORT_TMP/pipeline.txt" "$RUNTIME_DIR/requirements.txt" <<'PY'
"""Merge uv-export requirements files: join continuation lines, drop comments, dedupe.

Both exports come from the same uv.lock, so a package pinned in both must resolve to the
same version + hash set; only the environment marker may differ (e.g. one export carries
`; sys_platform != 'emscripten'` and the other is unconditional). Same name with a different
pin or hashes is an error. When markers differ, the weaker requirement wins (no marker),
or the markers are OR-combined.
"""
import re
import sys


def logical_lines(path):
    joined = []
    buf = ""
    for raw in open(path, encoding="utf-8"):
        line = raw.rstrip("\n")
        if not buf and (not line.strip() or line.lstrip().startswith("#")):
            continue
        if line.endswith("\\"):
            buf += line[:-1].rstrip() + " "
            continue
        buf += line
        # uv puts "# via ..." comments on their own (continued) lines; drop trailing comments
        buf = re.sub(r"\s+#.*$", "", buf).strip()
        if buf:
            joined.append(buf)
        buf = ""
    if buf.strip():
        joined.append(buf.strip())
    return joined


def parse(entry):
    """-> (name, pin, marker|None, sorted hash tuple). pin = 'name==version' as written."""
    tokens = entry.split()
    hashes = tuple(sorted(t for t in tokens if t.startswith("--hash=")))
    spec = " ".join(t for t in tokens if not t.startswith("--hash="))
    pin, _, marker = spec.partition(";")
    pin = pin.strip()
    marker = marker.strip() or None
    m = re.match(r"^([A-Za-z0-9._-]+)", pin)
    if not m:
        sys.exit(f"unparsable requirement: {entry}")
    return m.group(1).lower().replace("_", "-"), pin, marker, hashes


merged = {}  # name -> (pin, marker|None, hashes)
for path in sys.argv[1:3]:
    for entry in logical_lines(path):
        name, pin, marker, hashes = parse(entry)
        if name not in merged:
            merged[name] = (pin, marker, hashes)
            continue
        prev_pin, prev_marker, prev_hashes = merged[name]
        if prev_pin != pin or prev_hashes != hashes:
            sys.exit(
                f"requirements merge conflict for {name!r}:\n"
                f"  {prev_pin} {prev_hashes}\n  {pin} {hashes}"
            )
        if prev_marker is None or marker is None:
            marker = None
        elif prev_marker != marker:
            marker = f"({prev_marker}) or ({marker})"
        merged[name] = (pin, marker, hashes)

with open(sys.argv[3], "w", encoding="utf-8") as out:
    out.write("# Merged from `uv export --frozen --no-dev --no-emit-workspace` for\n")
    out.write("# --package narumi-server --extra secure and --package narumi --extra whisper-mlx --extra claude"
              " --extra anthropic --extra html --extra slides\n")
    for name in sorted(merged):
        pin, marker, hashes = merged[name]
        parts = [pin]
        if marker:
            parts.append(f"; {marker}")
        parts.extend(hashes)
        out.write(" ".join(parts) + "\n")
print(f"requirements.txt: {len(merged)} pinned requirements")
PY
  rm -rf "$EXPORT_TMP"

  # contracts（サーバーが起動時に読む契約。NARUMI_CONTRACTS_DIR で指す）
  echo "==> contracts"
  python3 "$INVENTORY" copy-contracts "$ROOT/contracts" "$RUNTIME_DIR/contracts" >/dev/null

  # manifest.json（再同期要否の判定材料）
  echo "==> runtime manifest.json"
  python3 - "$RUNTIME_DIR" "$VERSION" "$UV_VERSION" <<'PY'
import hashlib
import json
import pathlib
import sys

runtime = pathlib.Path(sys.argv[1])


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


wheels = {p.name: sha256(p) for p in sorted(runtime.glob("wheels/*.whl"))}
if len(wheels) != 2:
    sys.exit(f"expected 2 wheels (narumi, narumi-server), found: {sorted(wheels)}")
manifest = {
    "app_version": sys.argv[2],
    "python": "3.13",
    "uv_version": sys.argv[3],
    "wheels": wheels,
    "requirements_sha256": sha256(runtime / "requirements.txt"),
}
(runtime / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
print(json.dumps(manifest, indent=2, ensure_ascii=False))
PY
fi

# --- Info.plist ----------------------------------------------------------------------------
SU_PUBLIC_KEY_ENTRY=""
if [[ -n "$PUBLIC_KEY" ]]; then
  SU_PUBLIC_KEY_ENTRY="  <key>SUPublicEDKey</key>
  <string>${PUBLIC_KEY}</string>"
fi

cat > "$BUNDLE/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleDevelopmentRegion</key>
  <string>ja</string>
  <key>CFBundleExecutable</key>
  <string>NarumiMenuBar</string>
  <key>CFBundleIconFile</key>
  <string>AppIcon</string>
  <key>CFBundleIdentifier</key>
  <string>${BUNDLE_ID}</string>
  <key>CFBundleInfoDictionaryVersion</key>
  <string>6.0</string>
  <key>CFBundleName</key>
  <string>${APP_NAME}</string>
  <key>CFBundleDisplayName</key>
  <string>${APP_NAME}</string>
  <key>CFBundlePackageType</key>
  <string>APPL</string>
  <key>CFBundleShortVersionString</key>
  <string>${VERSION}</string>
  <key>CFBundleVersion</key>
  <string>${BUILD_NUMBER}</string>
  <key>LSMinimumSystemVersion</key>
  <string>15.0</string>
  <key>LSUIElement</key>
  <true/>
  <key>NSHighResolutionCapable</key>
  <true/>
  <key>NSMicrophoneUsageDescription</key>
  <string>会議の自分の発話をマイクトラックとして録音するために使用します。</string>
  <key>NSScreenCaptureUsageDescription</key>
  <string>会議の画面とシステム音声を議事録生成のために録画します。</string>
  <key>SUFeedURL</key>
  <string>${FEED_URL}</string>
${SU_PUBLIC_KEY_ENTRY}
  <key>SUEnableAutomaticChecks</key>
  <true/>
  <key>SUAutomaticallyUpdate</key>
  <false/>
  <key>SUScheduledCheckInterval</key>
  <integer>86400</integer>
</dict>
</plist>
PLIST
plutil -lint -s "$BUNDLE/Contents/Info.plist" || fail "Info.plist が不正です"
printf 'APPL????' > "$BUNDLE/Contents/PkgInfo"

# --- artifact inventory --------------------------------------------------------------------
echo "==> checking bundle inventory"
INVENTORY_ARGS=(check-app "$BUNDLE")
if [[ $WITH_RUNTIME -eq 1 ]]; then
  INVENTORY_ARGS+=(--require-runtime)
fi
if [[ -n "${NARUMI_TRACKED_SOURCES:-}" ]]; then
  INVENTORY_ARGS+=(--tracked-sources "$NARUMI_TRACKED_SOURCES")
fi
python3 "$INVENTORY" "${INVENTORY_ARGS[@]}" >/dev/null \
  || fail "配布対象外のファイル、または不正な runtime が bundle に含まれています"

# --- codesign ------------------------------------------------------------------------------
# Sparkle 公式の順: Autoupdate → Updater.app → Sparkle.framework → 同梱バイナリ → .app。
# .app への codesign が主実行ファイル NarumiMenuBar の署名そのもの。--deep は使わない。
echo "==> codesign (identity: ${IDENTITY})"
if [[ $WITH_RUNTIME -eq 1 ]]; then
  codesign "${SIGN_FLAGS[@]}" "$RUNTIME_DIR/uv"
fi
if [[ -d "$SPARKLE_DST" ]]; then
  codesign "${SIGN_FLAGS[@]}" "$SPARKLE_DST/Versions/B/Autoupdate"
  codesign "${SIGN_FLAGS[@]}" "$SPARKLE_DST/Versions/B/Updater.app"
  codesign "${SIGN_FLAGS[@]}" "$SPARKLE_DST"
fi
# The Keychain helper keeps the default per-executable ACL and needs no audio access.
codesign "${SIGN_FLAGS[@]}" "$BUNDLE/Contents/MacOS/narumi-keychain"
# Audio Input is needed only by the app and recorder, never by the other helpers.
codesign "${SIGN_FLAGS[@]}" --entitlements "$RECORDING_ENTITLEMENTS" \
  "$BUNDLE/Contents/MacOS/narumi-recorder"
codesign "${SIGN_FLAGS[@]}" --entitlements "$RECORDING_ENTITLEMENTS" "$BUNDLE"
codesign --verify --strict --verbose=1 "$BUNDLE/Contents/MacOS/narumi-keychain"
for recording_code in "$BUNDLE" "$BUNDLE/Contents/MacOS/narumi-recorder"; do
  codesign --verify --strict --verbose=1 "$recording_code"
  codesign --display --entitlements - --xml "$recording_code" 2>/dev/null \
    | validate_recording_entitlements \
    || fail "署名済みの録音用 entitlement を検証できません: $recording_code"
done

echo "built: $BUNDLE (version $VERSION, build $BUILD_NUMBER)"
