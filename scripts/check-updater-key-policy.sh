#!/usr/bin/env bash
# scripts/check-updater-key-policy.sh — Sparkle 鍵ポリシーの検査（リリース前提条件）。
#
# コミット済みの app/sparkle-public-key.txt と、Keychain の Sparkle 秘密鍵から導出した
# 公開鍵（generate_keys -p）が一致することを検査する。
#
# 不一致は --allow-pubkey-rotation 付きでのみ許可する。その場合のリリースは
# 「旧鍵で署名した橋渡し版」になる: Keychain の旧鍵で署名し（既存ユーザーが検証できる）、
# Info.plist には app/sparkle-public-key.txt の新公開鍵を埋め込んで、次版以降を新鍵に移行する。
#
# Usage: scripts/check-updater-key-policy.sh [--allow-pubkey-rotation]
# Env:   SPARKLE_BIN  generate_keys を含むディレクトリ（必須）
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PUBLIC_KEY_FILE="$ROOT/app/sparkle-public-key.txt"
ALLOW_ROTATION=0

fail() {
  echo "check-updater-key-policy: $*" >&2
  exit 1
}

for arg in "$@"; do
  case "$arg" in
    --allow-pubkey-rotation) ALLOW_ROTATION=1 ;;
    -h|--help)
      sed -n '2,/^set -euo pipefail/p' "${BASH_SOURCE[0]}" | sed '$d' | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *) fail "unknown argument: $arg" ;;
  esac
done

[[ -n "${SPARKLE_BIN:-}" ]] || fail "SPARKLE_BIN が未設定です（Sparkle の bin ディレクトリ）"
[[ -x "$SPARKLE_BIN/generate_keys" ]] || fail "$SPARKLE_BIN/generate_keys がありません"

[[ -f "$PUBLIC_KEY_FILE" ]] || fail "app/sparkle-public-key.txt がありません。初回リリース前に
  1) リリース担当者が手元で「$SPARKLE_BIN/generate_keys」を一度だけ実行して鍵を作成し（Keychain に保存される）、
  2) 表示された公開鍵を app/sparkle-public-key.txt にコミットし、
  3) 「generate_keys -x <退避先>」で秘密鍵をバックアップする（紛失 = 既存ユーザーへの更新手段の永久喪失）。
  スクリプトが鍵を自動生成することはない。"

committed="$(tr -d '[:space:]' < "$PUBLIC_KEY_FILE")"
[[ -n "$committed" ]] || fail "app/sparkle-public-key.txt が空です"

keychain="$("$SPARKLE_BIN/generate_keys" -p)" \
  || fail "Keychain から Sparkle 公開鍵を取得できません（generate_keys -p）。鍵が未作成なら上記の初回手順を実施してください"
keychain="$(printf '%s' "$keychain" | tr -d '[:space:]')"
[[ -n "$keychain" ]] || fail "generate_keys -p の出力が空です"

if [[ "$committed" == "$keychain" ]]; then
  echo "check-updater-key-policy: OK（公開鍵一致）"
  exit 0
fi

echo "check-updater-key-policy: 公開鍵が一致しません" >&2
echo "  app/sparkle-public-key.txt: $committed" >&2
echo "  Keychain (generate_keys -p): $keychain" >&2

if [[ $ALLOW_ROTATION -eq 1 ]]; then
  cat >&2 <<'EOF'
check-updater-key-policy: WARNING: --allow-pubkey-rotation により続行します。
  この版は「橋渡し版」です: 署名は Keychain の鍵（旧鍵）で行われ、既存ユーザーは
  それで検証します。Info.plist には app/sparkle-public-key.txt の公開鍵（新鍵）が
  入るため、この版を経由したユーザーは次版から新鍵で検証します。
  次版のリリース前に Keychain の鍵を新鍵へ入れ替えておくこと。
EOF
  exit 0
fi

fail "ローテーションを意図する場合のみ --allow-pubkey-rotation を付けて再実行してください（橋渡し版になります）"
