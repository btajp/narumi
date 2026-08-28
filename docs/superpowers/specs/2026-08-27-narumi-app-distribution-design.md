# narumi.app 配布・自動更新設計（2026-08-27）

目的: `narumi.app` を solo-eikaiwa と同じ体験（起動時に更新確認 → ダイアログ → DL・検証・差し替え・再起動）で配布し、**Python 側（pipeline / server）も含めて**更新されるようにする。更新機構は Sparkle（決定 2026-08-27）。

前提となる決定（0.1.1 の配布要件を 2026-08-28 に反映）:

- 配布経路は GitHub Releases のみ（Mac App Store は使わない）。Developer ID Application 署名＋Apple 公証＋staple を完了してから公開する
- 対象は Apple Silicon（arm64）・macOS 15 以降。mlx-whisper が Apple Silicon 専用のため x86_64 は配布しない
- アプリと Python パッケージの更新は `.app` の差し替えを入口にする。依存は同梱した lock 情報から取得する。ffmpeg / ffprobe は別途必要

## 1. ランタイム同梱（チェックアウト不要化）

Python 本体と ML 依存（mlx / torch 等）を `.app` に埋め込むと数百 MB〜GB になり、公証（内部の全 Mach-O を個別署名）も難しい。そこで **`.app` には「ブートストラップに必要な最小限」だけを同梱し、venv は `NARUMI_HOME/runtime/` に作る**。venv 内のファイルは quarantine が付かないので Gatekeeper の対象外。

`.app/Contents/Resources/runtime/` の内容:

| ファイル | 役割 |
|---|---|
| `uv`（単体バイナリ、`uv-aarch64-apple-darwin.tar.gz` から。版を `scripts/runtime.lock.json` に固定し sha256 検証） | Python 取得・venv 作成・依存インストール |
| `wheels/narumi-<ver>-py3-none-any.whl` / `wheels/narumi_server-<ver>-py3-none-any.whl`（`uv build` の出力） | pipeline / server 本体 |
| `requirements.txt`（`uv export --frozen --no-dev --no-emit-workspace --format requirements-txt` を `--package narumi-server` と `--package narumi --extra whisper-mlx --extra claude --extra anthropic --extra html --extra slides` で出力して結合・重複排除。ハッシュ付き） | サードパーティ依存の完全固定 |
| `contracts/`（manifest に列挙した契約と defs だけをコピー） | サーバーが起動時に読む契約。`NARUMI_CONTRACTS_DIR` で指す。未管理ファイルは含めない |
| `manifest.json` `{ "app_version", "python": "3.13", "uv_version", "wheels": {name: sha256}, "requirements_sha256" }` | 再同期要否の判定材料 |

初回起動（および manifest の内容が変わった更新後）の手順（`ServerLauncher` の bundled モード）:

1. `UV_PYTHON_INSTALL_DIR=<NARUMI_HOME>/runtime/python uv python install 3.13`
2. `uv venv <NARUMI_HOME>/runtime/venv.new --relocatable --python 3.13`
3. `uv pip install --python <venv.new> --require-hashes -r requirements.txt` → `uv pip install --python <venv.new> --no-deps wheels/*.whl`
4. journal を記録し、旧 venv を `venv.previous` に保持して新版へ切り替える。旧 installed marker は保持する
5. 新 server の応答、版、recorder/contracts の実体を照合した後に `installed.json` を確定する。失敗時は旧環境を復元し、次回起動時も未完了 journal から回復できるようにする

サーバー起動コマンド（bundled モード）: `<venv>/bin/narumi-server --http --host 127.0.0.1 --port <port> --recorder <app>/Contents/MacOS/narumi-recorder`、環境変数 `NARUMI_CONTRACTS_DIR=<app>/Contents/Resources/runtime/contracts`、`NARUMI_HOME` は既定のまま。

モード選択: 明示的な `NARUMI_RUNTIME_MODE=repo|bundled` → 明示的な `NARUMI_REPO` → 同梱 runtime → 保存済み設定や配置から解決した repo → 未設定。配布版は保存済み repoPath だけで開発ソースへ切り替わらない。bundled モードで外部 URL の明示がないときは、未所有の既存サーバーを採用せずポート競合にする。準備中はメイン画面とメニューに状態を表示し、ログは `~/Library/Logs/narumi/runtime.log`。失敗時は状態 `failed` と再試行操作を出し、黙って別モードへ切り替えない。

初回は Python と ML 依存をダウンロードする。torch は現行 mlx-whisper の依存として含まれる。pyannote は同梱しない。ffmpeg / ffprobe は既存のインストールを使用し、GUI 起動後の診断で検出を確認する。

## 2. 更新機構: Sparkle 2

- 依存: SwiftPM で `https://github.com/sparkle-project/Sparkle`（2.9.6 以上、binaryTarget の xcframework）。`NarumiMenuBar` のみが依存する。リンク時に `-Xlinker -rpath -Xlinker @executable_path/../Frameworks`
- `Info.plist`: `SUFeedURL = https://github.com/btajp/narumi/releases/latest/download/appcast.xml`、`SUPublicEDKey = <generate_keys の公開鍵>`、`SUEnableAutomaticChecks = YES`（2 回目起動時の許可ダイアログを出さない）、`SUAutomaticallyUpdate = NO`（黙って入れ替えず通知する。solo-eikaiwa と同じ）、`SUScheduledCheckInterval = 86400`。`CFBundleShortVersionString` = semver、`CFBundleVersion` = 単調増加の整数（リリーススクリプトが `git rev-list --count HEAD` を使う）
- コード: `AppDelegate` が `DesktopUpdater` を持ち、その内部で `SPUStandardUpdaterController` と各更新段階の busy gate を管理する。メニュー「アップデートを確認…」も同じ経路を使う。定期チェックは Sparkle のスケジューラに任せる。delegate の `feedURLString(for:)` は `NARUMI_SPARKLE_FEED_URL` があればそれを返す（開発用。本番設定には入れない）
- 更新確認・適用は録画中、開始停止中、起動準備中、既知ジョブの処理中には延期する。適用時は通常の終了経路で管理中 server を停止する。新バージョン起動時は manifest 差分で venv を再同期し、server の実体確認後に切替を確定する
- 別の CLI / MCP クライアントによるジョブ開始と更新の原子的な排他は保証しない。今回の保護範囲はアプリが開始・把握したジョブで、外部クライアントの処理中は更新しない
- `.app` 組み立て（`scripts/build-app.sh`）: Sparkle を `Contents/Frameworks/` にコピーし、非サンドボックス構成に不要な XPCServices を除く。Sparkle helpers → framework → 同梱 uv・recorder → `.app` の内側から順に署名する。署名に `--deep` は使わず、正式版は Developer ID と hardened runtime / timestamp を必須にする

## 3. 署名・公証・リリース（`scripts/release-app.sh <version>`）

solo-eikaiwa の `scripts/release-desktop.sh` と同じ流儀。手順:

1. 前提検査: PR マージ済み clean main と origin/main の一致、`gh` 認証、署名・公証資格情報、名前付き Keychain の Sparkle 鍵。Sparkle ツールは `SPARKLE_BIN` または既存 SwiftPM artifacts を使う。ローカル release.env の内容は出力しない
2. 版整合: `VERSION` と pyprojects、Python の実行時版、recorder の版、CHANGELOG の見出しを照合し、lock を確定する。アプリは `Info.plist` の版を表示する
3. 鍵ポリシー: `SPARKLE_KEY_ACCOUNT`（既定 `jp.btajp.narumi`）から得た公開鍵と、コミット済み `app/sparkle-public-key.txt`、配布アプリ内の公開鍵が一致すること。不一致は拒否する。鍵の移行は別途設計・検証が必要
4. ビルド: 稼働中の `dist/narumi.app` と分離した release 作業領域で wheel・依存・契約・アプリを構築する。ビルド・検証・公証・ZIP 化は同じアプリを参照する
5. 検証: `codesign --verify --deep --strict`、`spctl -a -t exec -vv`、`Info.plist` の版と `SUPublicEDKey` の再確認
6. 公証: `ditto -c -k --sequesterRsrc --keepParent narumi.app narumi-<ver>.zip` → `xcrun notarytool submit --wait` → `xcrun stapler staple narumi.app` → zip を作り直す
7. フィード: 最終 ZIP を固定して appcast を生成する。ZIP 内部の許可リスト、wheel 内の追跡済みソース、版/build、署名、length、当該 repo/tag の enclosure URL を検査し、ZIP と appcast の hash を確定する
8. draft: ZIP と appcast だけを対象 commit の GitHub draft Release に添付し、tag・commit・asset 集合・取得後の hash を照合する。draft と同一 ZIP で Applications 起動・診断を確認してから公開する。公開後に匿名の latest feed/ZIP とアプリの更新確認を検証する

公開前の検証に失敗したら公開しない。GUI 起動に失敗した場合は保持した旧アプリと runtime へ戻す。公開後に再取得しても hash が不一致なら当該新規 Release を draft に戻して調査し、公開済み asset を上書きせず新しい版で修正する。

秘密情報（Apple API キー・Sparkle 秘密鍵・パスワード）はリポ・Issue・ログに書かない。Keychain とリリース環境の env だけで扱う。

## 4. 更新 E2E（ローカル、Apple 資格情報不要）

solo-eikaiwa の `desktop/e2e-updater` と同型。`app/e2e-updater/README.md` に手順を置く:

現行ハーネスは実行停止中。更新後の再起動で既定データルートへ接続し得る点に加え、候補版 `99.0.0` と Python wheel の版が一致せず readiness に失敗する。副作用前に停止し、隔離と実版の一致を満たす改修後にのみ再開する。以下は当初の検証設計であり、現行版の成功記録ではない。同版の「最新」表示は、旧版から新版への実更新成功とは区別する。

1. 新版（`99.0.0`）を ad-hoc 署名で組み、`ditto` で zip、`generate_appcast` でローカル配信ディレクトリに `appcast.xml`
2. 旧版（現行版）を `/private/tmp/narumi-e2e/narumi.app` に置く（実パス。symlink 配下では検証しない）
3. `python3 -m http.server 8930` で配信し、`NARUMI_SPARKLE_FEED_URL=http://127.0.0.1:8930/appcast.xml NARUMI_HOME=<scratch> /private/tmp/narumi-e2e/narumi.app/Contents/MacOS/NarumiMenuBar` で起動
4. 合格条件: 更新ダイアログ → 適用 → 再起動後に `Info.plist` の版が `99.0.0`、`pgrep -fl narumi_server` が新プロセス 1 本のみ、`NARUMI_HOME/runtime/installed.json` が新 manifest に更新されている

## 5. 範囲外・注意

- Mac App Store 配布、x86_64 配布、delta 更新の細かな最適化
- pyannote のランタイム同梱（機密会議向けのローカル話者分離は別途 opt-in 手順）
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
