#!/usr/bin/env bash
# Build the Swift package in release mode and assemble dist/narumi.app
# (menu bar UI + narumi-recorder helper) with a minimal Info.plist and an ad-hoc signature.
#
# Usage: scripts/build-app.sh [--skip-build]
# Env:   DIST_DIR (default <repo>/dist)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_DIR="$ROOT/app"
DIST_DIR="${DIST_DIR:-$ROOT/dist}"
APP_NAME="narumi"
BUNDLE_ID="jp.btajp.narumi"
BUNDLE="$DIST_DIR/$APP_NAME.app"
VERSION="0.1.0"
SKIP_BUILD=0

for arg in "$@"; do
  case "$arg" in
    --skip-build) SKIP_BUILD=1 ;;
    -h|--help)
      sed -n '2,7p' "$0"
      exit 0
      ;;
    *)
      echo "unknown argument: $arg" >&2
      exit 2
      ;;
  esac
done

if [[ "$SKIP_BUILD" -eq 0 ]]; then
  echo "==> swift build -c release"
  (cd "$APP_DIR" && swift build -c release)
fi

BIN_DIR="$APP_DIR/.build/release"
for bin in NarumiMenuBar narumi-recorder; do
  if [[ ! -x "$BIN_DIR/$bin" ]]; then
    echo "missing binary: $BIN_DIR/$bin (run without --skip-build)" >&2
    exit 1
  fi
done

echo "==> assembling $BUNDLE"
rm -rf "$BUNDLE"
mkdir -p "$BUNDLE/Contents/MacOS" "$BUNDLE/Contents/Resources"
cp "$BIN_DIR/NarumiMenuBar" "$BUNDLE/Contents/MacOS/NarumiMenuBar"
cp "$BIN_DIR/narumi-recorder" "$BUNDLE/Contents/MacOS/narumi-recorder"
chmod 755 "$BUNDLE/Contents/MacOS/NarumiMenuBar" "$BUNDLE/Contents/MacOS/narumi-recorder"

cat > "$BUNDLE/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleDevelopmentRegion</key>
  <string>ja</string>
  <key>CFBundleExecutable</key>
  <string>NarumiMenuBar</string>
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
  <string>${VERSION}</string>
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
</dict>
</plist>
PLIST
printf 'APPL????' > "$BUNDLE/Contents/PkgInfo"

echo "==> codesign (ad-hoc)"
codesign --force --sign - --deep "$BUNDLE"
codesign --verify --verbose=1 "$BUNDLE"

echo "built: $BUNDLE"
