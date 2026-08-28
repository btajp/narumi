# narumi app（Swift Package）

録画 MVP（Step 1）の Swift 側。4 つのターゲットからなる。

| ターゲット | 種別 | 役割 |
|---|---|---|
| `NarumiRecorderKit` | ライブラリ | ScreenCaptureKit キャプチャ → AVAssetWriter で **別ファイル** 書き出し。イベント型・引数解析・ディスプレイ選択などの純粋ロジックはこの中の SCK 非依存な型に置き、`swift test` で検証する |
| `narumi-recorder` | CLI | server がサブプロセスとして起動する録画ヘルパー。stdout に JSON Lines でイベントを出す |
| `NarumiMenuBarCore` | ライブラリ（Foundation のみ） | メニューバーアプリの純粋ロジック。サーバー設定の解決（`ServerConfig`。ランタイムモード判定を含む）、起動コマンドの組み立て（`ServerCommand`）、サーバー状態と表示文言（`ServerState` / `ServerStatusText`）、同梱ランタイムの manifest と同期手順（`RuntimeManifest` / `RuntimeSyncPlan`）、契約レスポンスのモデル（`ContractModels`）、表示整形（`Formatting`）、議事録 markdown のブロック分割（`MarkdownBlocks`）、ツール名の一覧（`ToolCatalog`）。AppKit にも Sparkle にも依存せず `swift test` で検証する |
| `NarumiMenuBar` | メニューバーアプリ `narumi.app` | MCP クライアント。メニューバー（録画開始 / 停止）とメインウィンドウ（後述）から server の公開ツール（`ToolCatalog.allUsed` = 契約の全 27 ツール）を呼ぶだけで、ファイルや recorder には触れない（AGENTS.md 絶対原則 3）。加えて `narumi-server` の**プロセス**を起動・停止する（`ServerLauncher`。後述「起動フロー」） |

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

- `NSStatusItem`: 単色の状態アイコンと `narumi` の名前を表示する。メニューは「narumi を開く」/「録画開始…」「録画停止」/「サーバー: <状態>」/「サーバーを再起動」「リポジトリを選択…」「ログを開く」/「アップデートを確認…」/「終了」。
- 「録画開始…」は `NSAlert` で会議名（必須）・プロファイル・scope（どちらも空なら既定）を聞き、`start_recording {meeting_name, profile?, scope?, request_id}` を呼ぶ。「録画停止」は `stop_recording {request_id}`。`request_id` は操作ごとに UUID を発番し、成否不明のジョブ要求を再送するときは同じ ID と引数を使う。「録画開始…」はサーバーが稼働中（または外部サーバーに接続中）のときだけ有効。録画中は 5 秒ごとの `get_recording_status` で経過時間を「録画停止（h:mm:ss）」に表示し、他のクライアントが停止した場合も状態が追従する。
- 「サーバー: …」はサーバーが稼働中 / 外部サーバーに接続中のとき 5 秒ごとに `get_server_info` を呼んで更新する（`server_version` / `contract_version` があれば表示。`capabilities.recording` が false なら「録画不可」）。それ以外の状態（起動中・停止・起動失敗・未設定）はランチャーの状態をそのまま表示する。
- 接続先は `NARUMI_SERVER_URL` があればそれ、無ければ `http://127.0.0.1:<NARUMI_SERVER_PORT または 8765>/mcp`（起動するサーバーのポートと必ず一致する）。`MCPClient` は `initialize`（protocolVersion `2025-06-18`）→ `notifications/initialized` → `tools/call` を JSON-RPC 2.0 の POST で行い、`Mcp-Session-Id` を保持して送り返す。応答は `application/json` と `text/event-stream`（`data:` 行から同じ id の応答を取り出す）の両方を受け付ける。
- ツールエラー（`isError` / 構造化 `{"error":{code,message}}`）は `NSAlert` で表示する。

## メインウィンドウ（「narumi を開く」）

起動時に表示する SwiftUI ウィンドウ。閉じた後もメニューバーの「narumi を開く」で再表示できる。パリティ表（`docs/superpowers/specs/2026-08-27-narumi-surface-parity-design.md`「アプリの画面・操作一覧」）の全行を実装し、**データ操作はすべて MCP ツール**（`ToolCatalog` の定数のみ。AGENTS.md 絶対原則 3）。MCP 外の操作は、ツールが返したパスの Finder 表示と、サーバープロセス管理 / Sparkle（`MainWindowModel.HostActions` として AppDelegate が注入）だけ。

- **構成**: `MainWindowView`（`NSWindow` + `NSHostingView`。AppDelegate が生成・保持）。上部に文字付きの録画開始操作と準備状態、録画中は経過時間と停止操作。左に会議一覧サイドバー、右に会議詳細のタブ（議事録 / 文字起こし / コンテキスト / 設定）。ツールバーに「ジョブ」「取り込み」「プロファイル」「Gaia 接続」「診断」。
- **アクティベーション**: 起動時にウィンドウを開き `.regular`（Dock に出る）になる。閉じると `.accessory` のメニューバー常駐へ戻る。録画状態はメニューとメイン画面で共有し、操作より古いポーリング応答は採用しない。通信失敗を非録画へ変換せず、状態が確定するまで再開始を抑止する。
- **会議一覧**: scope フィルタ（空白区切り。空 = scope なしのみ）と検索フィールド。チェックボックスで `list_meetings --query`（会議名・engagement）と `search_transcripts`（カタログ FTS の発話全文検索。ヒットを開くとその会議の文字起こしタブへ）を切り替える。行には状態と進行中ジョブ（`active_job`）のバッジ。
- **録画中バナー**: `get_recording_status` が active のとき会議名・経過時間と「録画停止」（`stop_recording`）を表示。
- **議事録タブ**: `get_minutes`（版ピッカーで `available_versions` を切替）。markdown は `MarkdownParser`（NarumiMenuBarCore）でブロック分割して描画。`unresolved_speakers` があれば実名未解決のコールアウト。「再生成」は force / reason 付きで `regenerate` → 返った job_id をジョブ一覧で追跡し、完了時に表示を再読込。「エクスポート」は `list_export_destinations` の宛先メニューで、markdown / html は `NSSavePanel` で保存先を選んで `export_minutes {options: {output_path, overwrite}}`。版の履歴とエクスポート履歴（`get_meeting`）も表示。
- **文字起こしタブ**: `get_transcript`。ソースピッカー（merged / own-mic / own-system / ext-*）とセグメント表（タイムコード・話者名解決・全文選択可）。
- **コンテキストタブ**: 登録済み一覧（`get_meeting.contexts`）と登録フォーム（`register_context`）。テキスト貼り付け / URL / ファイル（選択またはウィンドウへのドロップ）、source_type、ラベル、auto_regenerate。
- **設定タブ**: `set_meeting_config` のフォーム。エンジン / LLM / 送信ポリシーの候補は `get_server_info.capabilities` から。危険な操作として `discard_tracks`（トラック選択 + 確認）と `delete_meeting`（確認。trash/ へ移動）。
- **取り込みシート**: `import_recording`（mic / system / screen のパス、プロファイル、scope、copy / auto_process）。
- **プロファイルシート**: `list_profiles` / `get_profile` / `set_profile`（make_default・自動エクスポート先を含む）/ `delete_profile`。
- **Gaia 接続シート**: `get_gaia_connection` / `set_gaia_connection` / `test_gaia_connection`。URL・write-only API キーを専用設定へ保存し、保存済み設定だけを接続テストする。同じ URL でキー欄が空なら維持、URL 変更時は旧キーを解除する。キーの明示削除・連携無効化も可能。キー入力は保存成功・失敗・シートを閉じる際に消去し、プロファイルや会議バンドルには渡さない。環境変数由来か保存済みか、接続先の契約版・クライアント名・既定 scope を表示する。
- **診断シート**: `get_server_info` の capabilities / diagnostics（ffmpeg・権限・データルートなど）、`rebuild_catalog`、そして MCP 外のプロセス操作（サーバー再起動 / ログを開く / アップデート確認）。
- **ジョブ**: ウィンドウが開始したジョブ + 一覧の `active_job` を追跡し、ツールバーにバッジ表示。ポップオーバーから `cancel_job`。
- **エラー表示**: 契約の error_envelope を code + message のアラートで表示。`busy` だけは非モーダルなトーストにする。

純粋ロジック（契約レスポンスのモデル `ContractModels`、表示整形 `NarumiFormat` / `MeetingRowPresentation`、markdown 分割 `MarkdownParser`）は `NarumiMenuBarCore` にあり、`ContractModelsTests`（`contracts/tools/*.json` の examples.output をそのままデコード）・`FormattingTests`・`MarkdownBlocksTests` で検証する。

## 起動フロー（narumi.app がサーバーを起動する）

`narumi.app` を開くと、メニューバー UI 自身が `narumi-server` の**プロセス**を起動し、終了時に停止する。Terminal は不要（録画ボタンを押すだけ）。データ操作はこれまで通りすべて MCP ツール呼び出しで、アプリはバンドル・カタログ・録画ファイルに触れない（AGENTS.md 絶対原則 3）。新しい責務は「サーバープロセスの起動と停止」だけで、`ServerLauncher`（AppKit 側）と `NarumiMenuBarCore` の純粋ロジックに分かれている。

1. **外部サーバーの検出**: repo モードまたは明示的な `NARUMI_SERVER_URL` がある場合は既存サーバーへ接続できる。そのプロセスはアプリの終了時にも停止しない。通常の bundled 起動では未所有の既存サーバーを採用せず、ポート競合として表示する。
2. **リポジトリの解決**（repo モード時。優先順）: 環境変数 `NARUMI_REPO` → `UserDefaults` の `narumi.repoPath` → `<repo>/dist/narumi.app` の配置からの推定 → 未設定。repo モードでは `pyproject.toml` と `server/pyproject.toml` が必要。配布版のモード選択は後述の判定順に従い、保存した repoPath より同梱 runtime を優先する。
3. **起動コマンド**: `/bin/zsh -lc 'exec uv run --project "$1" narumi-server --http --host 127.0.0.1 --port "$2" --recorder "$3"' narumi-server <repo> <port> <recorder>`。リポジトリ・ポート・recorder はシェル文字列に埋め込まず**位置パラメータ**（argv）で渡す（空白や日本語を含むパスでも壊れない）。環境変数で渡さないのは、ログインシェルの `~/.zshenv` / `~/.zprofile` がアプリの渡した環境の**後**に評価されるため: プロファイルに `export NARUMI_REPO=…` 等があると黙って上書きされ、別チェックアウトの server が起動したり、アプリがポーリングしないポートで bind されたりする。位置パラメータはプロファイルから書き換えられない。ログインシェル（`-l`）なので `~/.local/bin` / `/opt/homebrew/bin` / nix プロファイルなど利用者の PATH にある `uv` が使える（GUI アプリは launchd の最小限の PATH しか継承しない）。`--recorder` は同梱の `Contents/MacOS/narumi-recorder` があるときだけ付け、無ければ server 側の探索（`NARUMI_RECORDER` → `app/.build/{release,debug}/narumi-recorder`）に任せる。`NARUMI_HOME` が設定されていればそのまま引き継ぐ。カレントディレクトリはリポジトリ。stdin は `/dev/null`。
4. **ポートと URL**: ポートは `NARUMI_SERVER_PORT`（既定 8765）。接続先 URL は `NARUMI_SERVER_URL` があればそれ、無ければ `http://127.0.0.1:<port>/mcp`。`NARUMI_SERVER_URL` だけが設定されている場合はその URL のポートで起動する。
5. **起動待ち**: 1 秒ごとに `get_server_info` を最大 30 秒試し、応答したら「稼働中 v… 契約 …」。bundled モードは版・契約版・実体パスも照合する。失敗時は自分が起動したサーバーを停止し、未確定の runtime 切替を復元する。repo モードは、タイムアウト時も生きているプロセスを残し、終了または再起動で停止する。起動直後にプロセスが終了した場合も「起動失敗（ログ参照）」、稼働後に終了すると「停止 (exit N)」。

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

- **判定**（`ServerConfig.RuntimeMode`）: 明示的な `NARUMI_RUNTIME_MODE=repo|bundled` → 明示的な `NARUMI_REPO` → 同梱 runtime があれば **bundled** → リポジトリが解決できれば **repo** → 未設定。保存済み repoPath や配置場所だけでは同梱 runtime より優先しない。
- **bundled モード**: `.app` は `uv` バイナリ・`wheels/`・ハッシュ付き `requirements.txt`・`contracts/`・`manifest.json` を同梱する。起動時は既定のデータルート（または `NARUMI_HOME`）の `runtime/` に Python と venv を作る。`RuntimeSyncPlan` が Python の取得、relocatable な `venv.new` の作成、ハッシュ付き依存と本体 wheel のインストールを順に実行する。切替は journal に記録し、旧 venv を `venv.previous` に保持したまま新版を起動する。新 server の検証後に `installed.json` を確定し、旧 venv を片付ける。`--relocatable` は、移動後のエントリポイントが移動前のパスを参照しないために必要。
- **再同期と復旧**: 起動のたびに `Resources/runtime/manifest.json` と `installed.json` を比較し、不一致なら同期する。旧 venv と旧 marker は、新 server の応答・期待版・recorder/contracts の実体確認まで保持する。失敗時は復元し、未完了の切替 journal があれば次回起動時に回復する。旧版へ黙って接続を切り替えることはしない。
- **進捗・失敗**: 同期中はメニューに「サーバー: 環境を準備中…（<ステップ名>）」、uv の出力は `~/Library/Logs/narumi/runtime.log`。失敗（ネットワーク無しなど）は「起動失敗（ログ参照）」で止まり、「サーバーを再起動」で再試行する。黙って repo モードには落ちない。初回同期はネットワーク必須（Python 取得と PyPI から数百 MB）。
- **サーバー起動（bundled）**: `<venv>/bin/python3 -I -m narumi_server.cli --http --host 127.0.0.1 --port <port> --recorder <.app>/Contents/MacOS/narumi-recorder`、環境変数 `NARUMI_CONTRACTS_DIR=<.app>/Contents/Resources/runtime/contracts`。Python の環境上書きは除外し、旧 checkout や user site を import しない。`NARUMI_HOME` は引き継ぐ。uv とサーバーは、固定の `/bin/sh` gate で所有情報の保存を待ってから絶対パスで起動する（ログインシェルや PATH 探索は使わない）。異常終了後も前回のプロセスが生きていれば、勝手に停止したり venv を置換したりせず、再同期を止める。

## 自動更新（Sparkle）

- `NarumiMenuBar` のみが SwiftPM の [Sparkle](https://github.com/sparkle-project/Sparkle)（2.9.6 以上、binaryTarget）に依存する。`AppDelegate` が `DesktopUpdater` を保持し、その内部の `SPUStandardUpdaterController` に更新確認を渡す。定期チェックは Sparkle のスケジューラに任せ、確認・適用・終了の各段階でアプリの busy 状態を検査する（`Info.plist` の `SUEnableAutomaticChecks` / `SUScheduledCheckInterval` は `scripts/build-app.sh` が書く）。
- フィード: 通常は `Info.plist` の `SUFeedURL`。環境変数 `NARUMI_SPARKLE_FEED_URL` があれば delegate の `feedURLString(for:)` がそれを返す（ローカル更新 E2E 用。本番では設定しない）。
- 録画・開始停止・起動準備・既知ジョブの処理中は更新確認と適用を延期する。適用時は通常の終了経路で管理中 server を停止する。新バージョンの初回起動では manifest 差分により venv を再同期する。
- ジョブの保護対象はアプリが開始した、または一覧から把握した処理。別の CLI / MCP クライアントからのジョブ開始と更新の原子的な排他は未対応なので、その処理中に更新しない。
- 鍵: 更新の署名検証は `Info.plist` の `SUPublicEDKey`（`app/sparkle-public-key.txt` から埋め込む）。Keychain の `jp.btajp.narumi` account を使用する。`generate_keys --account jp.btajp.narumi -x <file>` でリポジトリ外へバックアップし、ファイルを所有者だけが読める状態にする。秘密鍵や Apple 資格情報はリポジトリ・ログに置かない。
