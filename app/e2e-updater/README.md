# 更新 E2E（ローカル、Apple 資格情報不要）

現在は実行停止中。`run-e2e.sh` はビルド・鍵生成・プロセス操作より前に終了する。
候補版 `99.0.0` と Python wheel の版が一致せず、新しい readiness 検証を通らないこと、
更新後の再起動で既定データルートを使うことが未対応のため。以下は改修時の参考となる
旧ハーネスの設計であり、現行版で実行できる手順や検証成功を示すものではない。

Sparkle 自動更新の一連の流れ（更新ダイアログ → DL・検証 → 差し替え → 再起動 → ランタイム再同期）を、
GitHub Releases も Apple 公証も使わずローカルだけで検証する（設計
`docs/superpowers/specs/2026-08-27-narumi-app-distribution-design.md` §4）。

このハーネスは専用の macOS 検証ユーザーでのみ実行する。更新後の再起動で環境変数が
引き継がれず、既定のデータルートを使うため、`E2E_DIR` だけでは普段の会議データや
正式インストール済みアプリから隔離できない。日常利用中のユーザーでは実行しない。

- 署名は ad-hoc。EdDSA 鍵は **E2E 専用の使い捨てシードファイル**（`head -c 32 /dev/urandom | base64`）を
  `sign_update` / `generate_appcast` の `--ed-key-file` に渡す。`generate_keys` は使わない —
  `generate_keys` は `-f` でもログイン Keychain に鍵を作る・触るため、E2E では一切呼ばない。
  本番鍵（Keychain / `app/sparkle-public-key.txt`）とは完全に分離される。
- 公開鍵は `derive-pubkey.py`（`uv run --no-project --with cryptography`）でシードから導出し、
  `SPARKLE_PUBLIC_KEY_FILE` 経由で `Info.plist` の `SUPublicEDKey` に埋め込む。
- フィードは `python3 -m http.server` によるローカル配信。アプリは環境変数
  `NARUMI_SPARKLE_FEED_URL` でそのフィードを見る（本番設定には入れない。updater delegate の
  `feedURLString(for:)` が読む）。

## 前提

- SwiftPM が取得した Sparkle 2.9.6 のツール（`generate_appcast` / `sign_update`）:

  ```sh
  cd app
  swift package resolve
  cd ..
  export SPARKLE_BIN="$PWD/app/.build/artifacts/sparkle/Sparkle/bin"
  ```

- Swift 側の Sparkle 統合（`Package.swift` の Sparkle 依存 + `SPUStandardUpdaterController` +
  `NARUMI_SPARKLE_FEED_URL` delegate）と `ServerLauncher` の bundled モードがマージ済みであること。
  未マージだと `run-e2e.sh` が前提検査（`otool -L` で Sparkle リンクを確認）で止まる。
- ネットワーク（初回のランタイム同期は uv の Python 取得と PyPI から数百 MB を落とす）。

## 実行

```sh
app/e2e-updater/run-e2e.sh
```

スクリプトが行うこと（更新ダイアログで「Install Update」を押す操作だけ手動）:

1. 使い捨て鍵を `$E2E_DIR/keys/` に生成（Keychain 不使用）
2. 旧版（現行 `VERSION`）と新版（`99.0.0`、`--version-override` + `--build-override`。Sparkle は
   `CFBundleVersion` で新旧を比較するため build 番号も上げる）を ad-hoc 署名・ランタイム同梱でビルド
3. 新版を `ditto` で zip → `generate_appcast --ed-key-file <seed>` でフィード生成
4. 旧版を `/private/tmp/narumi-e2e/narumi.app`（`E2E_DIR` で変更可。**実パス** — symlink 配下では
   Sparkle が検証しない）に置き、`python3 -m http.server 8930` で配信、
   `NARUMI_SPARKLE_FEED_URL=http://127.0.0.1:8930/appcast.xml NARUMI_HOME=$E2E_DIR/home` で起動
5. 更新適用・再起動（合格条件 1・2）を確認したら、**更新後アプリを引き取る**: Sparkle の再起動は
   環境変数を引き継がない（実 2.9.6 で確認）ため、再起動されたアプリは `NARUMI_HOME` を持たず
   既定データルート（`~/Library/Application Support/narumi`）へランタイム同期を始める。スクリプトは
   それを終了経路で止め、E2E 前に存在しなかった場合のみ既定データルートの `runtime/` を削除してから、
   更新後アプリを `NARUMI_HOME=$E2E_DIR/home` で起動し直す
6. 合格条件 3/4（ランタイム再同期・server 一本化）を検証し、終了時にアプリ・server・http.server を
   すべて停止する（何も残さない）

環境変数: `E2E_DIR`（既定 `/private/tmp/narumi-e2e`）/ `E2E_PORT`（既定 8930）/
`E2E_TIMEOUT`（既定 1800 秒）。

## 合格条件

1. 更新ダイアログ → 適用 → 再起動後、`Info.plist` の `CFBundleShortVersionString` が `99.0.0`
2. 更新後のアプリプロセスが起動している（Sparkle による自動再起動）
3. **ランタイム再同期**: 新版の `Contents/Resources/runtime/manifest.json` は旧版と異なる
   （少なくとも `app_version`）ため、新版の初回起動で venv が再同期され、
   `$NARUMI_HOME/runtime/installed.json` が新 manifest と一致する（Sparkle の再起動は env を
   引き継がないため、この条件はスクリプトが起動し直した更新後アプリで検証する）
4. `pgrep -fl narumi_server` が新プロセス 1 本のみ（旧 server が残っていない）

失敗時のログ: `$E2E_DIR/app.log`（アプリ）/ `$E2E_DIR/http-server.log`（フィード配信）/
`~/Library/Logs/narumi/runtime.log`（ランタイム同期。`NARUMI_HOME` を変えてもログは常に
ユーザーホーム配下）。
