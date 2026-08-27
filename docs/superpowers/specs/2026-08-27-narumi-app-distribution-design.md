# narumi.app 配布・自動更新設計（2026-08-27）

目的: `narumi.app` を solo-eikaiwa と同じ体験（起動時に更新確認 → ダイアログ → DL・検証・差し替え・再起動）で配布し、**Python 側（pipeline / server）も含めて**更新されるようにする。更新機構は Sparkle（決定 2026-08-27）。

前提となる決定:
- 配布経路は GitHub Releases のみ（Mac App Store は使わない）。Developer ID Application 署名＋Apple 公証＋staple を完了してから公開する
- 対象は Apple Silicon（arm64）・macOS 15 以降。mlx-whisper が Apple Silicon 専用のため x86_64 は配布しない
- 更新は `.app` の差し替えのみで完結させる。そのために `.app` を**自己完結**させる（下記「ランタイム同梱」）

## 1. ランタイム同梱（自己完結化）

Python 本体と ML 依存（mlx / torch 等）を `.app` に埋め込むと数百 MB〜GB になり、公証（内部の全 Mach-O を個別署名）も難しい。そこで **`.app` には「ブートストラップに必要な最小限」だけを同梱し、venv は `NARUMI_HOME/runtime/` に作る**。venv 内のファイルは quarantine が付かないので Gatekeeper の対象外。

`.app/Contents/Resources/runtime/` の内容:

| ファイル | 役割 |
|---|---|
| `uv`（単体バイナリ、`uv-aarch64-apple-darwin.tar.gz` から。版を `scripts/runtime.lock.json` に固定し sha256 検証） | Python 取得・venv 作成・依存インストール |
| `wheels/narumi-<ver>-py3-none-any.whl` / `wheels/narumi_server-<ver>-py3-none-any.whl`（`uv build` の出力） | pipeline / server 本体 |
| `requirements.txt`（`uv export --frozen --no-dev --no-emit-workspace --format requirements-txt` を `--package narumi-server` と `--package narumi --extra whisper-mlx --extra claude --extra anthropic --extra html` で出力して結合・重複排除。ハッシュ付き） | サードパーティ依存の完全固定 |
| `contracts/`（リポの `contracts/` のコピー） | サーバーが起動時に読む契約。`NARUMI_CONTRACTS_DIR` で指す |
| `manifest.json` `{ "app_version", "python": "3.13", "uv_version", "wheels": {name: sha256}, "requirements_sha256" }` | 再同期要否の判定材料 |

初回起動（および manifest の内容が変わった更新後）の手順（`ServerLauncher` の bundled モード）:

1. `UV_PYTHON_INSTALL_DIR=<NARUMI_HOME>/runtime/python uv python install 3.13`
2. `uv venv <NARUMI_HOME>/runtime/venv --python 3.13`（既存があれば作り直す。失敗しても旧 venv を壊さないよう `venv.new` に作って rename）
3. `uv pip install --python <venv> --require-hashes -r requirements.txt` → `uv pip install --python <venv> --no-deps wheels/*.whl`
4. `<NARUMI_HOME>/runtime/installed.json` に manifest の内容を書く。次回以降は一致すればスキップ

サーバー起動コマンド（bundled モード）: `<venv>/bin/narumi-server --http --host 127.0.0.1 --port <port> --recorder <app>/Contents/MacOS/narumi-recorder`、環境変数 `NARUMI_CONTRACTS_DIR=<app>/Contents/Resources/runtime/contracts`、`NARUMI_HOME` は既定のまま。

モード選択: `NARUMI_RUNTIME_MODE=repo|bundled` があればそれ。無ければ「リポ内 `dist/` で起動 or `NARUMI_REPO` あり → repo モード（`uv run` で起動、開発用）」「`Resources/runtime` あり → bundled モード」「どちらも無し → 未設定」。準備中はメニューに「サーバー: 環境を準備中…（Python 取得 / 依存インストール）」と進捗を出し、ログは `~/Library/Logs/narumi/runtime.log`。ネットワークが無くて失敗した場合は状態 `failed` と再試行メニューを出す（黙って repo モードに落ちない）。

初回のダウンロード量は数百 MB（mlx-whisper・mlx・numpy 等。torch / pyannote は含めない。pyannote は `narumi-dev doctor` 相当の案内で別途 opt-in）。

## 2. 更新機構: Sparkle 2

- 依存: SwiftPM で `https://github.com/sparkle-project/Sparkle`（2.9.6 以上、binaryTarget の xcframework）。`NarumiMenuBar` のみが依存する。リンク時に `-Xlinker -rpath -Xlinker @executable_path/../Frameworks`
- `Info.plist`: `SUFeedURL = https://github.com/btajp/narumi/releases/latest/download/appcast.xml`、`SUPublicEDKey = <generate_keys の公開鍵>`、`SUEnableAutomaticChecks = YES`（2 回目起動時の許可ダイアログを出さない）、`SUAutomaticallyUpdate = NO`（黙って入れ替えず通知する。solo-eikaiwa と同じ）、`SUScheduledCheckInterval = 86400`。`CFBundleShortVersionString` = semver、`CFBundleVersion` = 単調増加の整数（リリーススクリプトが `git rev-list --count HEAD` を使う）
- コード: `SPUStandardUpdaterController(startingUpdater: true, updaterDelegate: self, userDriverDelegate: nil)` を `AppDelegate` に持ち、メニューに「アップデートを確認…」（target = controller, action = `checkForUpdates:`）。起動時チェックは Sparkle のスケジューラに任せる。delegate で `feedURLString(for:)` を実装し、環境変数 `NARUMI_SPARKLE_FEED_URL` があればそれを返す（E2E 用。本番設定には入れない）
- 更新適用時の後始末: Sparkle は再起動前にアプリを終了させるので、`applicationShouldTerminate` の既存経路（録画中なら確認 → 管理中の server を SIGTERM → 終了）が走る。新バージョン起動時は manifest 差分で venv を再同期してから server を起動する
- `.app` 組み立て（`scripts/build-app.sh`）: `Sparkle.framework` を `Contents/Frameworks/` にコピー。サンドボックス非対応なので `Versions/B/XPCServices` は削除（公式手順「Removing XPC Services」）。署名順は Sparkle 公式どおり `Autoupdate` → `Updater.app` → `Sparkle.framework` → `narumi-recorder` → `NarumiMenuBar` → `.app`。`--deep` は使わない。`-o runtime --timestamp`（hardened runtime）。ad-hoc（`-`）でも同じ順で署名し、開発ビルドでも構造を崩さない

## 3. 署名・公証・リリース（`scripts/release-app.sh <version>`）

solo-eikaiwa の `scripts/release-desktop.sh` と同じ流儀。手順:

1. 前提検査: clean で push 済みの `main`、`gh` 認証、Sparkle ツール（`SPARKLE_BIN`、既定 `~/.sparkle/<ver>/bin`。無ければ `Sparkle-<ver>.tar.xz` の取得方法を案内して中断）、署名 env（`APPLE_SIGNING_IDENTITY` / `APPLE_API_KEY` / `APPLE_API_ISSUER` / `APPLE_API_KEY_PATH`）、Keychain の Sparkle 秘密鍵（`generate_keys -p` で公開鍵を取得できること）
2. 版整合: `VERSION` ファイルを正本にし、`pipeline/pyproject.toml` / `server/pyproject.toml` / `CHANGELOG.md` の見出しが一致することを検査（`scripts/check-version.sh`）。アプリの版は `Info.plist` から実行時に読む（Swift 側にハードコードしない）
3. 鍵ポリシー（`scripts/check-updater-key-policy.sh`）: `app/sparkle-public-key.txt`（コミット済み）と Keychain の公開鍵が一致すること。不一致は `--allow-pubkey-rotation` 付きでのみ許可し、その版は**旧鍵で署名した橋渡し版**にする（旧鍵で署名しなければ既存ユーザーが検証できない）。秘密鍵の紛失は既存ユーザーへの更新手段の永久喪失なので、`generate_keys -x` でのバックアップを README に明記
4. ビルド: `uv build` で wheel、`uv export` で requirements、`uv` バイナリ取得（sha256 検証）、`swift build -c release`、`build-app.sh --release`（ランタイム同梱・Sparkle 同梱・Developer ID 署名）
5. 検証: `codesign --verify --deep --strict`、`spctl -a -t exec -vv`、`Info.plist` の版と `SUPublicEDKey` の再確認
6. 公証: `ditto -c -k --sequesterRsrc --keepParent narumi.app narumi-<ver>.zip` → `xcrun notarytool submit --wait` → `xcrun stapler staple narumi.app` → zip を作り直す
7. フィード: `generate_appcast --download-url-prefix https://github.com/btajp/narumi/releases/download/v<ver>/ <dir>` で `appcast.xml`（＋delta）を生成。`sparkle:minimumSystemVersion 15.0`、`sparkle:hardwareRequirements arm64` を付ける。生成物の `sparkle:edSignature` を公開鍵で検証
8. 公開: `gh release create v<ver> --draft --target <sha> narumi-<ver>.zip appcast.xml *.delta --notes-file <CHANGELOG 抜粋>` → 手動確認後に publish。タグはスクリプトが作る（先に手で打たない）

秘密情報（Apple API キー・Sparkle 秘密鍵・パスワード）はリポ・Issue・ログに書かない。Keychain とリリース環境の env だけで扱う。

## 4. 更新 E2E（ローカル、Apple 資格情報不要）

solo-eikaiwa の `desktop/e2e-updater` と同型。`app/e2e-updater/README.md` に手順を置く:

1. 新版（`99.0.0`）を ad-hoc 署名で組み、`ditto` で zip、`generate_appcast` でローカル配信ディレクトリに `appcast.xml`
2. 旧版（現行版）を `/private/tmp/narumi-e2e/narumi.app` に置く（実パス。symlink 配下では検証しない）
3. `python3 -m http.server 8930` で配信し、`NARUMI_SPARKLE_FEED_URL=http://127.0.0.1:8930/appcast.xml NARUMI_HOME=<scratch> /private/tmp/narumi-e2e/narumi.app/Contents/MacOS/NarumiMenuBar` で起動
4. 合格条件: 更新ダイアログ → 適用 → 再起動後に `Info.plist` の版が `99.0.0`、`pgrep -fl narumi_server` が新プロセス 1 本のみ、`NARUMI_HOME/runtime/installed.json` が新 manifest に更新されている

## 5. 範囲外・注意

- Mac App Store 配布、x86_64 配布、delta 更新の細かな最適化
- pyannote / torch のランタイム同梱（機密会議向けのローカル話者分離は別途 opt-in 手順）
- 初回起動にネットワークが必要（uv の Python 取得と PyPI）。オフライン初回起動は非対応

## 実装状況メモ（2026-08-27、スクリプト側）

§1 のバンドル組み立て（`build-app.sh --runtime`、`scripts/runtime.lock.json` に uv 0.12.6 を sha256 固定）、§2 の `.app` 組み立て・署名順・`Info.plist` キー、§3 の `release-app.sh` / `check-version.sh` / `check-updater-key-policy.sh`、§4 の `app/e2e-updater/`（README + `run-e2e.sh`）を実装済み。実 Sparkle 2.9.6 のツール・実 uv 配布物・実 SwiftPM 展開物で検証した上での本文からの差分:

- **E2E の使い捨て鍵は `generate_keys` を使わない**: 実 2.9.6 の `generate_keys` は `-f` でもログイン Keychain に書き込む。E2E はランダム 32 バイトのシードファイル（`sign_update` / `generate_appcast` の `--ed-key-file` が読む「新形式」）を使い、公開鍵は `app/e2e-updater/derive-pubkey.py` で導出する（Keychain 完全不使用。実ツールで署名・検証を確認済み）
- **`spctl -a -t exec` は公証前は rejected が正常**なため、手順 5 では参考表示にとどめ、手順 6 の staple 後に必須検査する
- `build-app.sh` に **`--build-override`** を追加（E2E 用。Sparkle の新旧比較は `CFBundleVersion` なので、`--version-override 99.0.0` だけでは更新と認識されない）
- `generate_appcast` は `LSMinimumSystemVersion` と arm64 スライスから `sparkle:minimumSystemVersion` / `sparkle:hardwareRequirements` を自動付与する（実挙動で確認）。`release-app.sh` は欠けていた場合に備えて冪等に注入もする
- `sparkle:edSignature` の検証は `sign_update --verify`（Keychain の鍵から導出した公開鍵で検証）。手順 3 の鍵ポリシー検査と合わせて `app/sparkle-public-key.txt` に対する検証と等価
- **§1 の「torch は含めない」は現行ロックでは成立していない**: `uv export --extra whisper-mlx` が mlx-whisper 経由で torch を引き込む（`requirements.txt` に含まれる）。除外すると `--require-hashes` インストールが壊れるため、export 結果のまま同梱している。torch を外すには mlx-whisper の依存変更かロック調整が必要
- **手順 2 の venv は `--relocatable` で作る**（Swift 側 `RuntimeSyncPlan`）: 手順 2〜3 をすべて `venv.new` に対して行い最後に `venv` へ rename する実装のため、既定のシェバン（venv の絶対パスを埋め込む）では rename 後に `venv/bin/narumi-server` が消えた `venv.new/bin/python3` を指して exit 126 になる。`uv venv --relocatable` はエントリポイントを自己相対（`$(dirname $(realpath $0))/python3`）にする。実 .app のバンドル起動（bundled モード）で再現・修正を確認済み
- **Sparkle の再起動は環境変数を引き継がない**（実 2.9.6 の更新適用で確認）: 再起動されたアプリは `NARUMI_HOME` / `NARUMI_SPARKLE_FEED_URL` を持たず、既定データルートにランタイム同期を始める。§4 の合格条件 3・4 は、再起動の確認（条件 1・2）後に `run-e2e.sh` が更新後アプリを終了経路で止め、E2E 前に無かった場合のみ既定データルートの `runtime/` を片付け、E2E の env で起動し直して検証する方式に変更（本番動作には影響なし — 実運用の bundled アプリは env 無しで既定データルートに同期するのが正しい挙動）
