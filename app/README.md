# narumi app（Swift Package）

録画 MVP（Step 1）の Swift 側。4 つのターゲットからなる。

| ターゲット | 種別 | 役割 |
|---|---|---|
| `NarumiRecorderKit` | ライブラリ | ScreenCaptureKit キャプチャ → AVAssetWriter で **別ファイル** 書き出し。イベント型・引数解析・ディスプレイ選択などの純粋ロジックはこの中の SCK 非依存な型に置き、`swift test` で検証する |
| `narumi-recorder` | CLI | server がサブプロセスとして起動する録画ヘルパー。stdout に JSON Lines でイベントを出す |
| `NarumiMenuBarCore` | ライブラリ（Foundation のみ） | メニューバーアプリの純粋ロジック。サーバー設定の解決（`ServerConfig`。ランタイムモード判定を含む）、起動コマンドの組み立て（`ServerCommand`）、サーバー状態と表示文言（`ServerState` / `ServerStatusText`）、同梱ランタイムの manifest と同期手順（`RuntimeManifest` / `RuntimeSyncPlan`）。AppKit にも Sparkle にも依存せず `swift test` で検証する |
| `NarumiMenuBar` | メニューバーアプリ `narumi.app` | MCP クライアント。server の公開ツール（`start_recording` / `stop_recording` / `get_server_info`）を呼ぶだけで、ファイルや recorder には触れない（AGENTS.md 絶対原則 3）。加えて `narumi-server` の**プロセス**を起動・停止する（`ServerLauncher`。後述「起動フロー」） |

## ビルドとテスト

```sh
cd app
swift build                 # debug
swift build -c release      # app/.build/release/narumi-recorder を生成（server が探すパス）
swift test                  # XCTest（下記の注意を参照）
scripts/build-app.sh        # リポジトリ直下から。dist/narumi.app を組み立てて ad-hoc 署名
```

- 要件: macOS 15 以降、Swift 6.0 以降のツールチェーン。外部パッケージ依存は Sparkle（`NarumiMenuBar` のみが依存。初回ビルド時に GitHub から取得）だけで、引数解析は自前。
- **`swift test` には Xcode が必要**。`xcode-select -p` が `/Library/Developer/CommandLineTools` を指している環境では XCTest が無く `no such module 'XCTest'` になる。切り替えずに実行するには `DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer swift test`。ビルド（`swift build`）は Command Line Tools だけで通る。
- テストは ScreenCaptureKit・TCC・ネットワークに依存しない（イベント JSON の厳密一致、引数解析、ディスプレイ選択、ファイル名、`recorder.json` の書き出し。`NarumiMenuBarCore` はリポジトリ解決の優先順位、起動コマンドの argv / 環境変数、ポートからの URL 導出、ログパス、状態遷移と表示文言）。

## パリティ検査（アプリ ⊆ 契約）

アプリが呼ぶ MCP ツール名は `NarumiMenuBarCore` の `ToolCatalog` に集約する（`ToolCatalog.startRecording` / `.stopRecording` / `.getServerInfo`、一覧は `ToolCatalog.allUsed`）。`NarumiMenuBar` はツール呼び出しに文字列リテラルではなくこの定数を使う。`ToolCatalogTests` が `#filePath` からリポジトリルート（`contracts/manifest.json` を含むディレクトリ）を探して manifest を読み、`allUsed` の全ツールが契約に存在すること・一覧が空でなく重複もないことを `swift test` で検査する（ネットワーク不要）。アプリに新しいツール呼び出しを足すときは、必ず `ToolCatalog` に定数を追加して `allUsed` に載せる（AGENTS.md 絶対原則 3、`docs/superpowers/specs/2026-08-27-narumi-surface-parity-design.md`）。

## `narumi-recorder` CLI

```
narumi-recorder record --output <dir> [--display <id>] [--no-video] [--mic <device-uid>]
narumi-recorder check
narumi-recorder list-displays
narumi-recorder help
```

- `record`: `<dir>` を作成し、`screen.mp4`（H.264、幅 1920 上限、10 fps）/ `system.m4a`（AAC 48 kHz ステレオ 128 kbps、自プロセス音声は除外）/ `mic.m4a`（AAC 48 kHz モノラル 96 kbps）を **別ファイル** で書く。`--no-video` で `screen.mp4` を省く。`--display` 省略時は最初のディスプレイ、`--mic` は `AVCaptureDevice.uniqueID`。
- 停止条件: `SIGINT` / `SIGTERM`、または stdin に `stop` 行。stdin が **パイプ** で EOF になった場合（親プロセス消滅）も停止する。`/dev/null` や TTY の EOF は無視する。
- 終了時に `<dir>/recorder.json` を書く（`stopped` イベントの内容 + `started_at` + `recorder_version`。失敗時は `error` も入る）。
- 終了コード: 正常 0 / 録画失敗 1 / 引数エラー 2。失敗時は必ず `error` イベントを出す。
- `check`: `{"screen_recording":"granted|denied","microphone":"granted|denied|unknown"}`。画面収録は CoreGraphics に「未確認」を問い合わせる API が無いため、未確認も `denied` になる。
- `list-displays`: `[{"id":1,"width":1728,"height":1117,"name":"Built-in Retina Display"}]`（幅・高さはポイント）。

### stdout のイベント（1 行 1 JSON、各行で flush）

```
{"event":"started","started_at":"2026-08-27T03:05:00Z","tracks":{"screen":"screen.mp4","mic":"mic.m4a","system":"system.m4a"}}
{"event":"stopped","stopped_at":"2026-08-27T03:07:03Z","duration_sec":123.4,"tracks":{"screen":{"path":"screen.mp4","bytes":1234,"duration_sec":123.4},"mic":{"path":"mic.m4a","bytes":567,"duration_sec":123.4},"system":{"path":"system.m4a","bytes":890,"duration_sec":123.4}}}
{"event":"error","code":"permission_denied|no_display|capture_failed|writer_failed|invalid_argument","message":"..."}
{"event":"log","message":"..."}
```

- `--no-video` のときは `tracks` に `screen` が無い。
- 時刻は UTC・秒精度の ISO 8601。`duration_sec` はミリ秒で丸める。
- `started` の後にストリームが落ちた場合は `stopped` ではなく `error` を出し、書けたファイルはそのまま残す（`recorder.json` に `error` を記録）。
- 1 フレームも取れなかった `screen.mp4` は `bytes: 0` / `duration_sec: 0` で報告する（ファイルは存在しない）。

## TCC（画面収録・マイク）の扱い

TCC は「責任プロセス」単位で許可を記録する。

- **素の CLI（`app/.build/release/narumi-recorder`）**: Terminal から直接起動した場合は Terminal.app が責任プロセスになり、Terminal に対して画面収録・マイクの許可が求められる。server（`uv run narumi-server`）経由で起動した場合も、その server を起動した親アプリ（Terminal 等）が責任プロセスになる。初回の `record` で `SCShareableContent` の照会と `AVCaptureDevice.requestAccess` がプロンプトを出す。拒否されると `permission_denied` を返す（黙って続行しない）。launchd などプロンプトを出せない環境では、あらかじめ「システム設定 > プライバシーとセキュリティ」で許可しておく。
- **`.app`（`dist/narumi.app`）**: `Info.plist` に `NSMicrophoneUsageDescription` / `NSScreenCaptureUsageDescription` を持ち、`jp.btajp.narumi` として許可が記録される。`narumi.app` は server を自分で起動し、server には同梱の `Contents/MacOS/narumi-recorder` を `--recorder` で渡す（後述「起動フロー」）。この構成では recorder の祖先プロセスが `.app` なので、TCC の責任プロセスは `narumi.app` になり許可が .app に紐づく想定（実機での確認は未了）。
- ad-hoc 署名（`codesign --sign -`）はビルドのたびに署名が変わるため、TCC の許可が再度求められることがある。

## server から recorder を見つける方法

server の `RecordingController` は次の順で実行ファイルを探す。

1. 環境変数 `NARUMI_RECORDER`（絶対パス）
2. `app/.build/release/narumi-recorder`
3. `app/.build/debug/narumi-recorder`

いずれも無ければ `recorder_unavailable` エラー。開発時は `cd app && swift build -c release` を一度実行しておけばよい。`.app` 版を使う場合は `NARUMI_RECORDER=/path/to/dist/narumi.app/Contents/MacOS/narumi-recorder` を指定する。

## メニューバーアプリ（NarumiMenuBar）

- `NSStatusItem`: 待機中 🕵️ / 録画中 ⏺。メニューは「録画開始…」「録画停止」/「サーバー: <状態>」/「サーバーを再起動」「リポジトリを選択…」「ログを開く」/「終了」。
- 「録画開始…」は `NSAlert` で会議名を聞き、`start_recording {meeting_name, request_id}` を呼ぶ。「録画停止」は `stop_recording {request_id}`。`request_id` は UUID を毎回発番する。「録画開始…」はサーバーが稼働中（または外部サーバーに接続中）のときだけ有効。
- 「サーバー: …」はサーバーが稼働中 / 外部サーバーに接続中のとき 5 秒ごとに `get_server_info` を呼んで更新する（`server_version` / `contract_version` があれば表示。`capabilities.recording` が false なら「録画不可」）。それ以外の状態（起動中・停止・起動失敗・未設定）はランチャーの状態をそのまま表示する。
- 接続先は `NARUMI_SERVER_URL` があればそれ、無ければ `http://127.0.0.1:<NARUMI_SERVER_PORT または 8765>/mcp`（起動するサーバーのポートと必ず一致する）。`MCPClient` は `initialize`（protocolVersion `2025-06-18`）→ `notifications/initialized` → `tools/call` を JSON-RPC 2.0 の POST で行い、`Mcp-Session-Id` を保持して送り返す。応答は `application/json` と `text/event-stream`（`data:` 行から同じ id の応答を取り出す）の両方を受け付ける。
- ツールエラー（`isError` / 構造化 `{"error":{code,message}}`）は `NSAlert` で表示する。

## 起動フロー（narumi.app がサーバーを起動する）

`narumi.app` を開くと、メニューバー UI 自身が `narumi-server` の**プロセス**を起動し、終了時に停止する。Terminal は不要（録画ボタンを押すだけ）。データ操作はこれまで通りすべて MCP ツール呼び出しで、アプリはバンドル・カタログ・録画ファイルに触れない（AGENTS.md 絶対原則 3）。新しい責務は「サーバープロセスの起動と停止」だけで、`ServerLauncher`（AppKit 側）と `NarumiMenuBarCore` の純粋ロジックに分かれている。

1. **外部サーバーの検出**: 起動時にまず接続先 URL へ `get_server_info` を投げる。応答があれば状態は「外部サーバーに接続」になり、アプリは何も起動せず、終了時にも触れない。**アプリによるサーバー起動を無効にしたいときは、先に自分のサーバー（`scripts/dev.sh` など）を起動しておく。**
2. **リポジトリの解決**（repo モード時。優先順）: 環境変数 `NARUMI_REPO` → `UserDefaults` の `narumi.repoPath`（「リポジトリを選択…」で保存）→ バンドル位置のヒューリスティック（`.app` が `<repo>/dist/narumi.app` にあり、`<repo>/pyproject.toml` と `<repo>/server/pyproject.toml` が**両方**ある）→ 未設定。未設定なら「未設定（リポジトリを選択してください）」と表示して起動しない。指定先に 2 つのファイルが無い場合は「起動失敗（ログ参照）」。リポジトリが解決できないが `.app` がランタイムを同梱している場合は bundled モードになる（後述「ランタイムモード」）。
3. **起動コマンド**: `/bin/zsh -lc 'exec uv run --project "$1" narumi-server --http --host 127.0.0.1 --port "$2" --recorder "$3"' narumi-server <repo> <port> <recorder>`。リポジトリ・ポート・recorder はシェル文字列に埋め込まず**位置パラメータ**（argv）で渡す（空白や日本語を含むパスでも壊れない）。環境変数で渡さないのは、ログインシェルの `~/.zshenv` / `~/.zprofile` がアプリの渡した環境の**後**に評価されるため: プロファイルに `export NARUMI_REPO=…` 等があると黙って上書きされ、別チェックアウトの server が起動したり、アプリがポーリングしないポートで bind されたりする。位置パラメータはプロファイルから書き換えられない。ログインシェル（`-l`）なので `~/.local/bin` / `/opt/homebrew/bin` / nix プロファイルなど利用者の PATH にある `uv` が使える（GUI アプリは launchd の最小限の PATH しか継承しない）。`--recorder` は同梱の `Contents/MacOS/narumi-recorder` があるときだけ付け、無ければ server 側の探索（`NARUMI_RECORDER` → `app/.build/{release,debug}/narumi-recorder`）に任せる。`NARUMI_HOME` が設定されていればそのまま引き継ぐ。カレントディレクトリはリポジトリ。stdin は `/dev/null`。
4. **ポートと URL**: ポートは `NARUMI_SERVER_PORT`（既定 8765）。接続先 URL は `NARUMI_SERVER_URL` があればそれ、無ければ `http://127.0.0.1:<port>/mcp`。`NARUMI_SERVER_URL` だけが設定されている場合はその URL のポートで起動する。
5. **起動待ち**: 1 秒ごとに `get_server_info` を最大 30 秒試し、応答したら「稼働中 v… 契約 …」。30 秒で応答が無ければ「起動失敗（ログ参照）」— プロセスが生きていればそのまま残す（終了か再起動で止まる）。プロセスが起動直後に終了した場合（`uv` が見つからない、ポート使用中、リポジトリが `uv sync` 未了など）も「起動失敗（ログ参照）」。稼働後にプロセスが終了すると「停止 (exit N)」。

### サーバー関連のメニュー

- 「サーバーを再起動」: SIGTERM で停止してから起動し直す。外部サーバー / 未設定のときは無効。録画中なら確認ダイアログを出す（停止時に server が録画を確定するが、自動処理はされない）。
- 「リポジトリを選択…」: `NSOpenPanel` でディレクトリを選ぶ。`pyproject.toml` と `server/pyproject.toml` が無ければエラー。`UserDefaults`（`narumi.repoPath`）に保存して再起動する。`NARUMI_REPO` が設定されている間はそちらが優先される旨を表示する。
- 「ログを開く」: `~/Library/Logs/narumi/server.log` を既定のアプリで開く（無ければ作る）。server の stdout / stderr と、アプリ側の起動・停止メモ（`narumi.app:` 行）が同じファイルに入る。起動時に 5 MiB を超えていたら切り詰める。

### 終了

「終了」（⌘Q）、SIGTERM / SIGINT（`kill`、Terminal から起動した場合の Ctrl-C）はすべて同じ経路（`applicationShouldTerminate`）を通る。

1. このクライアントが開始した録画が進行中なら「録画中です。停止してから終了しますか？」→「停止して終了」で `stop_recording` を呼ぶ。`auto_process` は、アプリが起動したサーバーを直後に停止する場合は **false**（停止するサーバーに process ジョブを積んでも完走できず、SIGKILL で切られるだけ）、外部サーバーの場合は true（サーバーは生き続けてジョブを実行できる）。「キャンセル」で終了を取りやめる。
2. アプリが起動したサーバーがあれば SIGTERM を送り、最大 60 秒待つ。server は `--http` の SIGTERM を uvicorn の graceful shutdown（開いたままの MCP GET ストリームは 10 秒で打ち切り）の後に `ctx.close()` へ繋ぎ（`transports.graceful_sigterm`）、進行中の録画があれば確定する（recorder の停止タイムアウト 30 秒 + 終了猶予 10 秒 + トラックのハッシュ計算ぶん掛かりうる）。60 秒で終わらなければプロセスグループ（zsh→uv→python→recorder）ごと SIGKILL。
3. 外部サーバーには触れない。

## ランタイムモード（repo / bundled）

`narumi-server` の起動方法は 2 モード（設計: `docs/superpowers/specs/2026-08-27-narumi-app-distribution-design.md` §1）。

- **判定**（`ServerConfig.RuntimeMode`）: 環境変数 `NARUMI_RUNTIME_MODE=repo|bundled` があればそれ（他の値は無視して自動判定）→ リポジトリが解決できれば **repo**（上記「起動フロー」の `uv run`。開発用）→ `.app` に `Contents/Resources/runtime/` があれば **bundled** → どちらも無ければ未設定。
- **bundled モード**: `.app` はブートストラップ最小限（`uv` バイナリ・`wheels/`・ハッシュ付き `requirements.txt`・`contracts/`・`manifest.json`）だけを同梱し、起動時に `<NARUMI_HOME>/runtime/`（`NARUMI_HOME` 未設定時は `~/Library/Application Support/narumi`）へ venv を作る。手順は `RuntimeSyncPlan` が組み立てる固定列: ① `uv python install <ver>`（`UV_PYTHON_INSTALL_DIR=<home>/runtime/python`）② `uv venv <home>/runtime/venv.new --clear --relocatable --python <ver>` ③ `uv pip install --python <venv.new> --require-hashes -r requirements.txt` ④ `uv pip install --python <venv.new> --no-deps wheels/*.whl` ⑤ `venv.new` → `venv` へ差し替え ⑥ `manifest.json` を `installed.json` にコピー。すべて `venv.new` に入れてから最後に差し替えるので、途中で失敗しても旧 venv は壊れない。`--relocatable` は必須: エントリポイントのシェバンを自己相対にする（無いと `venv/bin/narumi-server` が rename 前の `venv.new/bin/python3` を指したまま exit 126 で死ぬ）。
- **再同期**: 起動のたびに `Resources/runtime/manifest.json` と `installed.json` を比較し、不一致（または `installed.json` 無し・読めない）なら同期し直す。アプリ更新後の初回起動はここで新しい依存が入る。
- **進捗・失敗**: 同期中はメニューに「サーバー: 環境を準備中…（<ステップ名>）」、uv の出力は `~/Library/Logs/narumi/runtime.log`。失敗（ネットワーク無しなど）は「起動失敗（ログ参照）」で止まり、「サーバーを再起動」で再試行する。黙って repo モードには落ちない。初回同期はネットワーク必須（Python 取得と PyPI から数百 MB）。
- **サーバー起動（bundled）**: `<venv>/bin/narumi-server --http --host 127.0.0.1 --port <port> --recorder <.app>/Contents/MacOS/narumi-recorder`、環境変数 `NARUMI_CONTRACTS_DIR=<.app>/Contents/Resources/runtime/contracts`。`NARUMI_HOME` は設定されていればそのまま引き継ぐ（repo モードと同じ）。シェルは使わない（venv も uv も絶対パスで PATH 不要）。停止・SIGKILL の扱いは repo モードと同一。

## 自動更新（Sparkle）

- `NarumiMenuBar` のみが SwiftPM の [Sparkle](https://github.com/sparkle-project/Sparkle)（2.9.6 以上、binaryTarget）に依存する。`AppDelegate` が `SPUStandardUpdaterController(startingUpdater: true, updaterDelegate: self, userDriverDelegate: nil)` を保持し、メニュー「アップデートを確認…」が `checkForUpdates:` を呼ぶ。定期チェックは Sparkle のスケジューラ任せ（`Info.plist` の `SUEnableAutomaticChecks` / `SUScheduledCheckInterval`。`scripts/build-app.sh` が書く）。
- フィード: 通常は `Info.plist` の `SUFeedURL`。環境変数 `NARUMI_SPARKLE_FEED_URL` があれば delegate の `feedURLString(for:)` がそれを返す（ローカル更新 E2E 用。本番では設定しない）。
- 更新適用時は Sparkle がアプリを終了させるため、通常の終了経路（録画の確認 → 管理中 server の停止）がそのまま走る。新バージョンの初回起動では manifest 差分により venv が再同期される。
- 鍵: 更新の署名検証は `Info.plist` の `SUPublicEDKey`（`app/sparkle-public-key.txt` から `build-app.sh` が埋め込む）。**Keychain の Sparkle 秘密鍵は `generate_keys -x <file>` でバックアップしておくこと** — 紛失すると既存ユーザーに更新を届ける手段を永久に失う。秘密鍵や Apple 資格情報はリポジトリ・ログに置かない。
