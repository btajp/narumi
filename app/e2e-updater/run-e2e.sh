#!/usr/bin/env bash
# app/e2e-updater/run-e2e.sh — Sparkle 更新のローカル E2E（設計 §4）。Apple 資格情報・Keychain 不要。
#
# 旧版（現行 VERSION）を E2E_DIR/narumi.app に置き、新版（99.0.0）をローカル HTTP フィードで
# 配信して「更新ダイアログ → 適用 → 再起動 → ランタイム再同期」を検証する。署名鍵は E2E 専用の
# 使い捨てシードファイルのみ（generate_keys は使わない — Keychain に鍵を作らない・触れない）。
#
# 更新ダイアログの操作（Install Update）だけは手動。それ以外（ビルド・鍵・フィード・配信・検証・
# 後始末）は自動で、終了時にプロセスは何も残らない。Sparkle の再起動は環境変数を引き継がない
# ため、再起動の確認（合格条件 2）後はスクリプトが更新後アプリを E2E 環境で起動し直して
# ランタイム再同期（合格条件 3/4）を検証する。
#
# Usage: app/e2e-updater/run-e2e.sh
# Env:
#   SPARKLE_BIN  generate_appcast / sign_update のディレクトリ（必須。例 ~/.sparkle/2.9.6/bin）
#   E2E_DIR      作業ディレクトリ（既定 /private/tmp/narumi-e2e。symlink 配下は不可 — Sparkle は
#                実パスでしか検証しない）
#   E2E_PORT     フィード配信ポート（既定 8930）
#   E2E_TIMEOUT  更新適用〜ランタイム再同期完了の待ち秒数（既定 1800。初回は数百 MB の DL を含む）
set -euo pipefail

# Keep the legacy harness inert until both restart isolation and wheel versions match.
printf '%s\n' 'run-e2e: 現在は実行停止中です。データルートの隔離と候補版 wheel の版合わせが未対応です（README 参照）。' >&2
exit 2

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
E2E_DIR="${E2E_DIR:-/private/tmp/narumi-e2e}"
E2E_PORT="${E2E_PORT:-8930}"
E2E_TIMEOUT="${E2E_TIMEOUT:-1800}"
NEW_VERSION="99.0.0"

fail() {
  echo "run-e2e: $*" >&2
  exit 1
}

step() {
  echo
  echo "===> $*"
}

# --- 前提検査 ------------------------------------------------------------------------------
step "前提検査"
[[ -n "${SPARKLE_BIN:-}" ]] || fail "SPARKLE_BIN が未設定です（Sparkle 2.9.6 の bin。README 参照）"
for tool in generate_appcast sign_update; do
  [[ -x "$SPARKLE_BIN/$tool" ]] || fail "$SPARKLE_BIN/$tool がありません"
done
command -v uv >/dev/null 2>&1 || fail "uv がありません"
command -v python3 >/dev/null 2>&1 || fail "python3 がありません"
case "$E2E_DIR" in
  /*) ;;
  *) fail "E2E_DIR は絶対パスで指定してください: $E2E_DIR" ;;
esac

APP="$E2E_DIR/narumi.app"
FEED_DIR="$E2E_DIR/feed"
KEYS_DIR="$E2E_DIR/keys"
HOME_DIR="$E2E_DIR/home"
PLIST="$APP/Contents/Info.plist"
FEED_URL="http://127.0.0.1:$E2E_PORT/appcast.xml"
HTTP_PID=""

# Sparkle の再起動は環境変数を引き継がない（実 2.9.6 で確認）: 再起動直後のアプリは
# NARUMI_HOME を持たず、既定データルートにランタイム同期を始める。合格条件 2 の後で
# スクリプトが引き取り、E2E が作った runtime/ だけを片付ける（E2E 前から存在した場合は触らない）。
DEFAULT_DATA_ROOT="$HOME/Library/Application Support/narumi"
DEFAULT_RUNTIME_PREEXISTED=0
if [[ -e "$DEFAULT_DATA_ROOT/runtime" ]]; then
  DEFAULT_RUNTIME_PREEXISTED=1
fi

# --- 後始末（成功・失敗どちらでも走る。何も走らせたまま残さない） ---------------------------
cleanup() {
  trap - EXIT INT TERM
  echo
  echo "===> 後始末"
  if [[ -n "$HTTP_PID" ]] && kill -0 "$HTTP_PID" 2>/dev/null; then
    kill -TERM "$HTTP_PID" 2>/dev/null || true
  fi
  # E2E のアプリ（更新後の再起動プロセス含む）を止める。パスで限定し、他の narumi には触れない。
  pkill -TERM -f "$APP/Contents/MacOS/NarumiMenuBar" 2>/dev/null || true
  # アプリの終了経路が自前サーバーを SIGTERM で畳むのを待つ（最大 90 秒）
  local i=0
  while [[ $i -lt 90 ]]; do
    pgrep -f "$APP/Contents/MacOS/NarumiMenuBar" >/dev/null 2>&1 || break
    sleep 1
    i=$((i + 1))
  done
  pkill -KILL -f "$APP/Contents/MacOS/NarumiMenuBar" 2>/dev/null || true
  # bundled モードの server は E2E の venv から起動される: そのパスでのみ落とす
  pkill -TERM -f "$HOME_DIR/runtime/venv" 2>/dev/null || true
  sleep 2
  pkill -KILL -f "$HOME_DIR/runtime/venv" 2>/dev/null || true
  # Sparkle 再起動後のアプリ（env 無し）が既定データルートに作ったランタイムを片付ける。
  # E2E 前から存在していた runtime/ は触らない（実運用のキャッシュかもしれない）。
  if [[ $DEFAULT_RUNTIME_PREEXISTED -eq 0 && -e "$DEFAULT_DATA_ROOT/runtime" ]]; then
    pkill -TERM -f "$DEFAULT_DATA_ROOT/runtime/venv" 2>/dev/null || true
    sleep 2
    pkill -KILL -f "$DEFAULT_DATA_ROOT/runtime/venv" 2>/dev/null || true
    rm -rf "$DEFAULT_DATA_ROOT/runtime"
    echo "run-e2e: 既定データルートの runtime/（E2E が作成）を削除しました"
  fi
  wait 2>/dev/null || true
  echo "run-e2e: cleanup done（作業物は $E2E_DIR に残る。鍵は使い捨て）"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# --- 使い捨て鍵（ファイルのみ。Keychain には作らない） --------------------------------------
step "使い捨て Ed25519 鍵の生成（${KEYS_DIR}）"
rm -rf "$APP" "$FEED_DIR" "$KEYS_DIR" "$HOME_DIR" "$E2E_DIR/build-old" "$E2E_DIR/build-new"
mkdir -p "$E2E_DIR"
(umask 077 && mkdir -p "$KEYS_DIR")
SEED_FILE="$KEYS_DIR/ed25519-seed"
(umask 077 && head -c 32 /dev/urandom | base64 > "$SEED_FILE")
PUBKEY_FILE="$KEYS_DIR/sparkle-public-key.txt"
uv run --no-project --with cryptography python "$SCRIPT_DIR/derive-pubkey.py" "$SEED_FILE" > "$PUBKEY_FILE"
echo "run-e2e: SUPublicEDKey = $(cat "$PUBKEY_FILE")"

# --- ビルド（旧 = 現行 VERSION、新 = 99.0.0。どちらも ad-hoc 署名 + ランタイム同梱） --------
CURRENT_VERSION="$(tr -d '[:space:]' < "$ROOT/VERSION")"
BASE_BUILD="$(git -C "$ROOT" rev-list --count HEAD)"
NEW_BUILD=$((BASE_BUILD + 1000000))  # Sparkle は CFBundleVersion で新旧を比較する

step "旧版 $CURRENT_VERSION (build $BASE_BUILD) をビルド"
SPARKLE_PUBLIC_KEY_FILE="$PUBKEY_FILE" DIST_DIR="$E2E_DIR/build-old" \
  "$ROOT/scripts/build-app.sh" --runtime

step "新版 $NEW_VERSION (build $NEW_BUILD) をビルド（バイナリは再利用）"
SPARKLE_PUBLIC_KEY_FILE="$PUBKEY_FILE" DIST_DIR="$E2E_DIR/build-new" \
  "$ROOT/scripts/build-app.sh" --runtime --skip-build \
  --version-override "$NEW_VERSION" --build-override "$NEW_BUILD"

# 旧版アプリは Sparkle が更新対象を検証できるよう実パスに置く
ditto "$E2E_DIR/build-old/narumi.app" "$APP"

# Sparkle 統合が Swift 側に入っているかの検査（無ければ更新ダイアログは出ない）
if ! otool -L "$APP/Contents/MacOS/NarumiMenuBar" | grep -q "Sparkle.framework"; then
  fail "NarumiMenuBar が Sparkle.framework にリンクしていません（Swift 側の Sparkle 統合が未マージ）"
fi

# --- フィード（zip + appcast.xml をローカル HTTP で配信） ----------------------------------
step "フィード生成（${FEED_DIR}）"
mkdir -p "$FEED_DIR"
ditto -c -k --sequesterRsrc --keepParent "$E2E_DIR/build-new/narumi.app" \
  "$FEED_DIR/narumi-$NEW_VERSION.zip"
"$SPARKLE_BIN/generate_appcast" \
  --ed-key-file "$SEED_FILE" \
  --download-url-prefix "http://127.0.0.1:$E2E_PORT/" \
  -o "$FEED_DIR/appcast.xml" \
  "$FEED_DIR"

step "ローカル配信開始（127.0.0.1:${E2E_PORT}）"
python3 -m http.server "$E2E_PORT" --bind 127.0.0.1 --directory "$FEED_DIR" \
  > "$E2E_DIR/http-server.log" 2>&1 &
HTTP_PID=$!
sleep 1
kill -0 "$HTTP_PID" 2>/dev/null || fail "http.server を起動できません（ポート $E2E_PORT 使用中？）"

# --- 旧版を起動して更新を待つ ---------------------------------------------------------------
step "旧版を起動（NARUMI_SPARKLE_FEED_URL=${FEED_URL}）"
NARUMI_SPARKLE_FEED_URL="$FEED_URL" NARUMI_HOME="$HOME_DIR" \
  "$APP/Contents/MacOS/NarumiMenuBar" > "$E2E_DIR/app.log" 2>&1 &

cat <<EOF

  ── 手動操作 ─────────────────────────────────────────────
  更新ダイアログが出たら「Install Update」で適用してください。
  すぐ出ない場合はメニューバーの narumi から「アップデートを確認…」。
  （適用 → 再起動 → ランタイム再同期まで最大 ${E2E_TIMEOUT} 秒待ちます）
  ─────────────────────────────────────────────────────────
EOF

deadline=$((SECONDS + E2E_TIMEOUT))

step "合格条件 1/4: Info.plist の版が $NEW_VERSION になるまで待機"
while :; do
  ver="$(/usr/libexec/PlistBuddy -c 'Print CFBundleShortVersionString' "$PLIST" 2>/dev/null || true)"
  [[ "$ver" == "$NEW_VERSION" ]] && break
  [[ $SECONDS -lt $deadline ]] || fail "タイムアウト: 版が $NEW_VERSION になりません（現在: ${ver:-?}）"
  sleep 2
done
echo "run-e2e: OK（$NEW_VERSION に更新された）"

step "合格条件 2/4: 更新後のアプリが再起動している"
pgrep -f "$APP/Contents/MacOS/NarumiMenuBar" >/dev/null \
  || fail "更新後のアプリプロセスが見つかりません"
echo "run-e2e: OK"

# --- 引き取り: Sparkle の再起動は E2E の環境変数を引き継がない ------------------------------
step "更新後アプリの引き取り（Sparkle の再起動は環境変数を引き継がない）"
# 再起動されたアプリは NARUMI_HOME / NARUMI_SPARKLE_FEED_URL を持たず、既定データルートへ
# ランタイム同期を始めている。終了経路（同期の中断・自前 server の停止つき）で止めてから、
# E2E の環境で起動し直して合格条件 3/4 を検証する。
pkill -TERM -f "$APP/Contents/MacOS/NarumiMenuBar" 2>/dev/null || true
i=0
while pgrep -f "$APP/Contents/MacOS/NarumiMenuBar" >/dev/null 2>&1; do
  if [[ $i -ge 90 ]]; then
    pkill -KILL -f "$APP/Contents/MacOS/NarumiMenuBar" 2>/dev/null || true
    break
  fi
  sleep 1
  i=$((i + 1))
done
if [[ $DEFAULT_RUNTIME_PREEXISTED -eq 0 && -e "$DEFAULT_DATA_ROOT/runtime" ]]; then
  echo "run-e2e: 既定データルートの runtime/（再起動アプリが作成）を削除します"
  rm -rf "$DEFAULT_DATA_ROOT/runtime"
elif [[ $DEFAULT_RUNTIME_PREEXISTED -eq 1 ]]; then
  echo "run-e2e: 注意: ${DEFAULT_DATA_ROOT}/runtime は E2E 前から存在するため削除しません" \
    "（再起動アプリが再同期した可能性あり。次回の bundled 起動で manifest 差分により自動復旧）"
fi

step "更新後アプリを E2E 環境で起動（NARUMI_HOME=${HOME_DIR}）"
NARUMI_SPARKLE_FEED_URL="$FEED_URL" NARUMI_HOME="$HOME_DIR" \
  "$APP/Contents/MacOS/NarumiMenuBar" >> "$E2E_DIR/app.log" 2>&1 &

step "合格条件 3/4: installed.json が新 manifest と一致するまで待機（ランタイム再同期）"
NEW_MANIFEST="$APP/Contents/Resources/runtime/manifest.json"
INSTALLED="$HOME_DIR/runtime/installed.json"
while :; do
  if python3 -c '
import json, sys
try:
    a = json.load(open(sys.argv[1]))
    b = json.load(open(sys.argv[2]))
except OSError:
    sys.exit(1)
sys.exit(0 if a == b else 1)
' "$INSTALLED" "$NEW_MANIFEST"; then
    break
  fi
  [[ $SECONDS -lt $deadline ]] || fail "タイムアウト: $INSTALLED が新 manifest と一致しません（runtime.log 参照）"
  sleep 5
done
echo "run-e2e: OK（ランタイム再同期完了）"

step "合格条件 4/4: narumi-server が新プロセス 1 本のみ"
server_count="$(pgrep -f "$HOME_DIR/runtime/venv" | wc -l | tr -d ' ')"
echo "run-e2e: E2E venv から起動された server プロセス:"
pgrep -fl "$HOME_DIR/runtime/venv" || true
[[ "$server_count" == "1" ]] || fail "E2E venv からの server プロセスが $server_count 本あります（期待: 1）"
echo "run-e2e: OK"

echo
echo "run-e2e: PASS — 更新適用・再起動・ランタイム再同期・server 一本化をすべて確認"
