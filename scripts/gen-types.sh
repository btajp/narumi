#!/usr/bin/env bash
# Generate pydantic v2 models from contracts/ into pipeline/src/narumi/contracts/_generated/.
#
# The output directory is gitignored: never commit it and never edit it by hand — fix the
# contract file and re-run this script. Requires the dev group (datamodel-code-generator) to be
# synced: `uv sync`.
#
# Layout of the output:
#   _generated/common.py      shared definitions from contracts/defs/common.json
#   _generated/<tool>.py      <Tool>Input / <Tool>Output models for each contracts/tools/<tool>.json
#   _generated/__init__.py    re-exports every module
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONTRACTS="$ROOT/contracts"
OUT="${NARUMI_GENERATED_DIR:-$ROOT/pipeline/src/narumi/contracts/_generated}"
COMMON_MODULE="narumi.contracts._generated.common"

if [[ ! -f "$CONTRACTS/manifest.json" ]]; then
  echo "gen-types: contracts/manifest.json not found under $ROOT" >&2
  exit 1
fi

# Flags shared by every invocation. --allow-remote-refs only silences the "remote" warning that
# datamodel-codegen emits for ../defs/common.json (a local file outside the tools/ base path);
# nothing is fetched over the network. --strict-refs turns unresolved $ref into an error.
common_flags=(
  --output-model-type pydantic_v2.BaseModel
  --target-python-version 3.12
  --use-annotated
  --use-schema-description
  --use-standard-collections
  --use-union-operator
  --disable-timestamp
  --enable-generated-header-marker
  --strict-refs
  --allow-remote-refs
  --formatters ruff-format
)

codegen() {
  uv run --project "$ROOT" datamodel-codegen "$@"
}

rm -rf "$OUT"
mkdir -p "$OUT"

# 1) Shared definitions → common.py
codegen \
  --input "$CONTRACTS/defs/common.json" \
  --input-file-type jsonschema \
  --output "$OUT/common.py" \
  "${common_flags[@]}"

# 2) One module per tool. datamodel-codegen's mcp-tools reader takes a single tool document (not a
#    directory), and --external-ref-mapping makes it import the shared types from common.py instead
#    of duplicating them in every module.
modules=(common)
for tool_file in "$CONTRACTS"/tools/*.json; do
  name="$(basename "$tool_file" .json)"
  codegen \
    --input "$tool_file" \
    --input-file-type mcp-tools \
    --output "$OUT/$name.py" \
    --external-ref-mapping "../defs/common.json=$COMMON_MODULE" \
    "${common_flags[@]}"
  modules+=("$name")
done

# 3) Package init re-exporting every module.
{
  echo '"""Generated from contracts/ by scripts/gen-types.sh. Do not edit; do not commit."""'
  echo
  for module in "${modules[@]}"; do
    echo "from narumi.contracts._generated import $module as $module"
  done
  echo
  echo "__all__ = ["
  for module in "${modules[@]}"; do
    echo "    \"$module\","
  done
  echo "]"
} > "$OUT/__init__.py"

echo "$OUT"
