#!/usr/bin/env bash
# Require the committed public key to match the explicitly selected Keychain account.
# Usage: scripts/check-updater-key-policy.sh
# Env: NARUMI_RELEASE_ENV, SPARKLE_BIN, SPARKLE_KEY_ACCOUNT (default jp.btajp.narumi).
# No key is generated, imported, exported, or rotated by this check.
set +x
set +v
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$ROOT/scripts/release-common.sh"

fail() {
  echo "check-updater-key-policy: $*" >&2
  exit 1
}

if [[ $# -gt 0 ]]; then
  case "$1" in
    -h|--help) echo "Usage: scripts/check-updater-key-policy.sh"; exit 0 ;;
    --allow-pubkey-rotation) fail "鍵不一致での出荷は許可しません。鍵移行は別途検証が必要です" ;;
    *) fail "unknown argument" ;;
  esac
fi

release_load_env || fail "リリース設定を読み込めません"
SPARKLE_BIN="$(release_sparkle_bin "$ROOT")" \
  || fail "Sparkle ツールがありません。SwiftPM の依存解決後、または SPARKLE_BIN 指定で再実行してください"
SPARKLE_KEY_ACCOUNT="${SPARKLE_KEY_ACCOUNT:-jp.btajp.narumi}"
PUBLIC_KEY_FILE="$ROOT/app/sparkle-public-key.txt"
[[ -f "$PUBLIC_KEY_FILE" ]] || fail "コミットする公開鍵が未設定です（app/sparkle-public-key.txt）"
committed="$(tr -d '[:space:]' < "$PUBLIC_KEY_FILE")"
[[ -n "$committed" ]] || fail "公開鍵が空です"

# -p only looks up an existing key. Always pass the app-specific account.
keychain="$("$SPARKLE_BIN/generate_keys" --account "$SPARKLE_KEY_ACCOUNT" -p 2>/dev/null)" \
  || fail "指定アカウントの Sparkle 公開鍵を取得できません"
keychain="$(printf '%s' "$keychain" | tr -d '[:space:]')"
[[ "$committed" == "$keychain" ]] || fail "コミット済み公開鍵と指定アカウントの公開鍵が一致しません"
echo "check-updater-key-policy: OK（公開鍵一致）"
