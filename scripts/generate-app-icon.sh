#!/bin/bash
# Regenerate the committed app icon from its SVG source using macOS tools only.
# Usage: bash scripts/generate-app-icon.sh [output-directory]
set -euo pipefail

if [[ $# -gt 1 ]]; then
    echo "Usage: $0 [output-directory]" >&2
    exit 2
fi

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
SOURCE="$REPO_ROOT/app/Assets/AppIcon.svg"
OUTPUT_DIR="${1:-$REPO_ROOT/app/Assets}"

for tool in sips iconutil; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        echo "Missing macOS tool: $tool" >&2
        exit 1
    fi
done
if [[ ! -s "$SOURCE" ]]; then
    echo "Missing icon source: $SOURCE" >&2
    exit 1
fi

ICON_TMP="$(mktemp -d "${TMPDIR:-/tmp}/narumi-app-icon.XXXXXX")"
trap 'rm -rf -- "$ICON_TMP"' EXIT
ICONSET="$ICON_TMP/AppIcon.iconset"
mkdir "$ICONSET"

# SVG defines a 1024px canvas with transparent corners. Downsample this master
# consistently rather than rasterizing each size with a different view box.
sips -s format png "$SOURCE" --out "$ICON_TMP/AppIcon.png" >/dev/null
for points in 16 32 128 256 512; do
    for scale in 1 2; do
        pixels=$((points * scale))
        suffix=""
        if [[ $scale -eq 2 ]]; then
            suffix="@2x"
        fi
        filename="icon_${points}x${points}${suffix}.png"
        if [[ $pixels -eq 1024 ]]; then
            cp "$ICON_TMP/AppIcon.png" "$ICONSET/$filename"
        else
            sips -z "$pixels" "$pixels" "$ICON_TMP/AppIcon.png" \
                --out "$ICONSET/$filename" >/dev/null
        fi
    done
done

iconutil -c icns "$ICONSET" -o "$ICON_TMP/AppIcon.icns"
mkdir -p "$OUTPUT_DIR"
cp "$ICON_TMP/AppIcon.png" "$OUTPUT_DIR/AppIcon.png"
cp "$ICON_TMP/AppIcon.icns" "$OUTPUT_DIR/AppIcon.icns"
echo "Generated $OUTPUT_DIR/AppIcon.icns and $OUTPUT_DIR/AppIcon.png"
