# narumi app（Swift Package）

録画・会議管理・接続設定を扱う Swift アプリ。製品コードは 5 つのターゲットからなる。この README は 0.3.0 の Codex 接続・モデル選択・テキスト議事録生成までを扱う。

| ターゲット | 種別 | 役割 |
|---|---|---|
| `NarumiRecorderKit` | ライブラリ | ScreenCaptureKit キャプチャ → AVAssetWriter で **別ファイル** 書き出し。イベント型・引数解析・ディスプレイ選択などの純粋ロジックはこの中の SCK 非依存な型に置き、`swift test` で検証する |
| `narumi-recorder` | CLI | server がサブプロセスとして起動する録画ヘルパー。stdout に JSON Lines でイベントを出す |
| `narumi-keychain` | 専用ヘルパー | プロバイダの API キーと常駐接続トークンを macOS Keychain で扱う。秘密情報は引数や環境変数に置かず、匿名パイプで受け渡す |
| `NarumiMenuBarCore` | ライブラリ（Foundation / Security） | サーバー設定・起動コマンド・状態表示、同梱ランタイムの同期手順、契約型、表示整形、markdown 分割、ツール名一覧、TLS 起動情報と証明書の検証、Keychain ヘルパーとの通信、プロバイダ設定・認可 URL・確認コード・議事録モデル選択の状態管理。AppKit / Sparkle に依存せず、I/O を差し替えて `swift test` で検証する |
| `NarumiMenuBar` | メニューバーアプリ `narumi.app` | MCP クライアント。メニューとメインウィンドウから server の公開ツール（`ToolCatalog.allUsed` = 契約 3.0.0 の全 37 ツール）を呼び、会議バンドルや recorder には直接触れない。加えて `narumi-server` のプロセス管理と、接続のための起動情報・Keychain トークンの取得を行う |

## 利用者向けの導入・更新

初回は既存版があれば先に [停止・退避](../README.md#初回導入と失敗時の復旧) を済ませ、[GitHub Releases](https://github.com/btajp/narumi/releases/latest) の DMG を開いて `narumi.app` を「Applications」へドラッグする。
以後は公開済みの更新をメニューバーの「narumi」→「アップデートを確認…」から適用する。
実環境確認は公開・更新後に行い、開発ビルドを毎回手動コピーしない。
導入・復旧の詳細は [README の配布手順](../README.md#初回導入と失敗時の復旧) を参照する。

## ビルドとテスト

```sh
cd app
swift build                 # debug
swift build -c release      # narumi-recorder / narumi-keychain / NarumiMenuBar を生成
swift test                  # XCTest（下記の注意を参照）
cd ..
scripts/build-app.sh        # dist/narumi.app を組み立て、両ヘルパーを同梱して ad-hoc 署名
```

- 要件: macOS 15 以降、Swift 6.0 以降のツールチェーン。外部パッケージ依存は Sparkle（`NarumiMenuBar` のみが依存。初回ビルド時に GitHub から取得）だけで、引数解析は自前。
- **`swift test` には Xcode が必要**。`xcode-select -p` が `/Library/Developer/CommandLineTools` を指している環境では XCTest が無く `no such module 'XCTest'` になる。切り替えずに実行するには `DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer swift test`。ビルド（`swift build`）は Command Line Tools だけで通る。
- 通常のテストは実録画・TCC 許可・実 Keychain・プロバイダへの通信を使わない。契約型、起動コマンド、TLS 起動情報、確認コード、秘密入力の消去、議事録モデル選択、応答喪失時の状態復旧を fake と一時ファイルで検証する。固定版 Codex CLI の loopback 検証では、外部通信を遮断して選択モデル・tools 空・正常完了・切断後の再送防止を確認した。実 ChatGPT ログイン・実生成、実 API・実 Keychain、アプリ更新後の画面操作は未検証。

## パリティ検査（アプリ ⊆ 契約）

アプリが呼ぶ MCP ツール名は `NarumiMenuBarCore` の `ToolCatalog` に集約する（`ToolCatalog.startRecording` / `.stopRecording` / `.getServerInfo`、一覧は `ToolCatalog.allUsed`）。`NarumiMenuBar` はツール呼び出しに文字列リテラルではなくこの定数を使う。`ToolCatalogTests` が `#filePath` からリポジトリルート（`contracts/manifest.json` を含むディレクトリ）を探して manifest を読み、`allUsed` の全ツールが契約に存在すること・一覧が空でなく重複もないことを `swift test` で検査する（ネットワーク不要）。アプリに新しいツール呼び出しを足すときは、必ず `ToolCatalog` に定数を追加して `allUsed` に載せる（AGENTS.md 絶対原則 3、`docs/superpowers/specs/2026-08-27-narumi-surface-parity-design.md`）。

## `narumi-keychain` ヘルパー

`scripts/build-app.sh` は `Contents/MacOS/narumi-keychain` を録画ヘルパーと同様に同梱・署名する。Keychain の service は `jp.btajp.narumi.secrets.v1` 固定で、操作は narumi 専用 account の取得・保存・削除に限定する。アプリもサーバーもこのヘルパーを経由し、秘密値をコマンド行や環境変数へ渡さない。

アプリがサーバーへ渡す `NARUMI_KEYCHAIN_HELPER` は実行ファイルの絶対パスであり、秘密値ではない。通常はアプリが解決するため利用者の設定は不要。ヘルパーが見つからない、Keychain が使えないなどの失敗では、平文ファイルへの保存や未認証通信に切り替えずエラーにする。Gaia 接続のキーは従来の `gaia.json` 保存のままで、この変更では移行しない。

Codex の ChatGPT 認証は API キー方式とは別で、接続ごとの narumi 専用認証領域を使う。アプリは公開 MCP ツールからログイン・ログアウトを要求し、既存の Codex アプリ・CLI の認証情報を読んだりコピーしたりしない。

## `narumi-recorder` CLI

```
narumi-recorder record --output <dir> [--display <id>] [--no-video] [--mic <device-uid>]
narumi-recorder check
narumi-recorder request-permission microphone|screen_recording
narumi-recorder open-permission-settings microphone|screen_recording
narumi-recorder list-displays
narumi-recorder help
```

- `record`: `<dir>` を作成し、`screen.mp4`（H.264、幅 1920 上限、10 fps）/ `system.m4a`（AAC 48 kHz ステレオ 128 kbps、自プロセス音声は除外）/ `mic.m4a`（AAC 48 kHz モノラル 96 kbps）を **別ファイル** で書く。`--no-video` で `screen.mp4` を省く。`--display` 省略時は最初のディスプレイ、`--mic` は `AVCaptureDevice.uniqueID`。
- 停止条件: `SIGINT` / `SIGTERM`、または stdin に `stop` 行。stdin が **パイプ** で EOF になった場合（親プロセス消滅）も停止する。`/dev/null` や TTY の EOF は無視する。
- 終了時に `<dir>/recorder.json` を書く（`stopped` イベントの内容 + `started_at` + `recorder_version`。失敗時は `error` も入る）。
- 終了コード: 正常 0 / 録画失敗 1 / 引数エラー 2。失敗時は必ず `error` イベントを出す。
- `check`: `{"screen_recording":"granted|denied","microphone":"granted|denied|unknown"}`。画面収録は CoreGraphics に「未確認」を問い合わせる API が無いため、未確認も `denied` になる。
- `request-permission`: 対象の OS 許可を要求するだけで、録画・出力ディレクトリ・トラックは作らない。拒否も現在状態を含む正常 JSON として返す。拒否済みのマイクはプロンプトが再表示されないため、設定から変更する。
- `open-permission-settings`: 対象から決まる固定のプライバシー設定 URL を開く。任意 URL は受け付けず、対象 URL を開けないときだけプライバシー設定トップへ戻る。応答の `settings_opened` は画面を開く要求の受付で、許可済みを意味しない。
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

利用者向けの入口はアプリの「診断」→「録画の権限」。マイク・画面収録の状態、許可を求めるボタン、macOS 設定を開くボタン、再確認をまとめる。許可要求は明示クリック時だけで、起動・診断・復帰時の確認では要求しない。録画入口でも未許可なら同じ画面へ案内する。設定の項目名や URL アンカーは OS 版で異なる場合があるため、手動の辿り方も画面に併記する。

アプリの操作は `configure_recording_permission` を通り、常駐 server が同梱 helper を起動する。許可待機・子プロセス回収中は録画開始・再起動・更新適用を保留し、通信断後に自動再要求しない。再接続して状態を確認できるか、所有するプロセス群の終了を確認できるまで保留を維持する。旧契約の server に新しい引数やツールは送らず、更新が必要と案内する。

- **素の CLI（`app/.build/release/narumi-recorder`）**: Terminal から直接起動した場合は Terminal.app が責任プロセスになり、Terminal に対して画面収録・マイクの許可が求められる。server（`uv run narumi-server`）経由で起動した場合も、その server を起動した親アプリ（Terminal 等）が責任プロセスになる。初回の `record` で `SCShareableContent` の照会と `AVCaptureDevice.requestAccess` がプロンプトを出す。拒否されると `permission_denied` を返す（黙って続行しない）。launchd などプロンプトを出せない環境では、あらかじめ「システム設定 > プライバシーとセキュリティ」で許可しておく。
- **`.app`（`dist/narumi.app`）**: `Info.plist` に `NSMicrophoneUsageDescription` / `NSScreenCaptureUsageDescription` を持ち、`jp.btajp.narumi` として許可が記録される。`narumi.app` は server を自分で起動し、server には同梱の `Contents/MacOS/narumi-recorder` を `--recorder` で渡す（後述「起動フロー」）。この構成では recorder の祖先プロセスが `.app` なので、TCC の責任プロセスは `narumi.app` になり許可が .app に紐づく想定（実機での確認は未了）。
- ad-hoc 署名（`codesign --sign -`）はビルドのたびに署名が変わるため、TCC の許可が再度求められることがある。

旧開発版から正式署名版へ移行した際、設定上は有効でも旧署名の許可が残り、権限の照合に失敗する場合がある。
再起動で改善しない場合は、対象 app の署名と TCC の診断ログを確認する。許可の整理が必要なら、対象権限を明示して利用者の承認を得る。
他アプリの許可やマイクを一括リセットせず、再許可の確定は利用者が行う。

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
- 接続先の既定は `https://127.0.0.1:<NARUMI_SERVER_PORT または 8765>/mcp`。`NARUMI_SERVER_URL` を指定する場合も数値 loopback の HTTPS に限り、所有者専用の起動情報と一致する必要がある。証明書の pin・有効期限・接続先を検証してから Keychain の Bearer トークンで認証し、環境プロキシやリダイレクトを使わない。`MCPClient` は `initialize`（protocolVersion `2025-06-18`）→ `notifications/initialized` → `tools/call` を JSON-RPC 2.0 の POST で行い、`Mcp-Session-Id` を保持する。応答は `application/json` と `text/event-stream` を受け付ける。対応する契約版と認証付き TLS のメタデータを確認できなければ操作を開始しない。Codex は契約 3.x が必要で、同梱サーバーは契約 3.0.0 と厳密に照合する。開発用の外部サーバー接続だけは契約 2 の従来操作も許可し、その場合は `minutes_model: null` と `expected_config` を送らない。
- ツールエラー（`isError` / 構造化 `{"error":{code,message}}`）は `NSAlert` で表示する。

## メインウィンドウ（「narumi を開く」）

起動時に表示する SwiftUI ウィンドウ。閉じた後もメニューバーの「narumi を開く」で再表示できる。**データ操作はすべて MCP ツール**（`ToolCatalog` の定数のみ。AGENTS.md 絶対原則 3）。MCP 外では、ツールが返したパスの Finder 表示、サーバープロセス管理 / Sparkle、接続のための起動情報と Keychain トークンの取得を行う。プロバイダ設定もアプリから直接ファイルへ保存せず、公開ツールを使う。

- **構成**: `MainWindowView`（`NSWindow` + `NSHostingView`。AppDelegate が生成・保持）。上部に文字付きの録画開始操作と準備状態、録画中は経過時間と停止操作。左に会議一覧サイドバー、右に会議詳細のタブ（議事録 / 文字起こし / コンテキスト / 設定）。ツールバーに「ジョブ」「取り込み」「プロファイル」「AI 接続」「Gaia 接続」「診断」。
- **アクティベーション**: 起動時にウィンドウを開き `.regular`（Dock に出る）になる。閉じると `.accessory` のメニューバー常駐へ戻る。録画状態はメニューとメイン画面で共有し、操作より古いポーリング応答は採用しない。通信失敗を非録画へ変換せず、状態が確定するまで再開始を抑止する。
- **会議一覧**: scope フィルタ（空白区切り。空 = scope なしのみ）と検索フィールド。チェックボックスで `list_meetings --query`（会議名・engagement）と `search_transcripts`（カタログ FTS の発話全文検索。ヒットを開くとその会議の文字起こしタブへ）を切り替える。行には状態と進行中ジョブ（`active_job`）のバッジ。
- **録画中バナー**: `get_recording_status` が active のとき会議名・経過時間と「録画停止」（`stop_recording`）を表示。
- **議事録タブ**: `get_minutes`（版ピッカーで `available_versions` を切替）。markdown は `MarkdownParser`（NarumiMenuBarCore）でブロック分割して描画。`unresolved_speakers` があれば実名未解決のコールアウト。「再生成」は確認後に `regenerate` を呼び、返った job_id を追跡して完了時に再読込。Codex 選択時は接続・モデル・推論量・試行番号・送信内容を表示し、確認した設定を `expected_config` に渡す。Codex では強制再生成を表示せず、明示的な試行番号の変更を使う。理由欄は任意。「エクスポート」は `list_export_destinations` の宛先メニューで、markdown / html は `NSSavePanel` で保存先を選んで `export_minutes {options: {output_path, overwrite}}`。版の履歴とエクスポート履歴（`get_meeting`）も表示。
- **文字起こしタブ**: `get_transcript`。ソースピッカー（merged / own-mic / own-system / ext-*）とセグメント表（タイムコード・話者名解決・全文選択可）。
- **コンテキストタブ**: 登録済み一覧（`get_meeting.contexts`）と登録フォーム（`register_context`）。テキスト貼り付け / URL / ファイル（選択またはウィンドウへのドロップ）、source_type、ラベル、auto_regenerate。Codex を選んだ会議で登録後の再生成を有効にする場合は、新しく登録する内容も送信対象に含むと表示し、確認した設定を `expected_config` に渡す。
- **設定タブ**: `set_meeting_config` のフォーム。「従来の LLM プロバイダ」は発話統合・画像読解などに使い、「議事録の生成方法」で選んだ Codex 接続・モデル・推論量はテキスト議事録だけに適用する。保存だけでは生成しない。外部送信ポリシーは自動変更せず、Codex では `subscription_ok` または `api_ok` の明示選択が必要。危険な操作として `discard_tracks`（トラック選択 + 確認）と `delete_meeting`（確認。trash/ へ移動）。
- **取り込みシート**: `import_recording`（mic / system / screen のパス、プロファイル、scope、copy / auto_process）。
- **プロファイルシート**: `list_profiles` / `get_profile` / `set_profile`（make_default・自動エクスポート先を含む）/ `delete_profile`。会議設定と同じ Codex 選択フォームを使う。保存だけでは生成せず、既存会議にも適用しない。選んだプロファイルでの新しい録画・取り込みは、自動処理の指定に従ってテキストを送信する。
- **AI 接続シート**: Codex App Server / Anthropic API / Claude Agent SDK / ローカル Ollama の接続追加・編集・有効化・無効化・削除、認証・ログアウト、実行環境の準備、モデル候補の取得・参照。`list_providers` / `list_provider_connections` / `set_provider_connection` / `delete_provider_connection` / `prepare_provider_runtime` / `authenticate_provider_connection` / `get_provider_auth_status` / `test_provider_connection` / `list_provider_models` の 9 ツールを使う。認証状態・モデル取得状態・生成状態を別々に表示する。
- **Gaia 接続シート**: `get_gaia_connection` / `set_gaia_connection` / `test_gaia_connection`。URL・write-only API キーを専用設定へ保存し、保存済み設定だけを接続テストする。同じ URL でキー欄が空なら維持、URL 変更時は旧キーを解除する。キーの明示削除・連携無効化も可能。キー入力は保存成功・失敗・シートを閉じる際に消去し、プロファイルや会議バンドルには渡さない。環境変数由来か保存済みか、接続先の契約版・クライアント名・既定 scope を表示する。
- **診断シート**: 先頭にマイク・画面収録の状態と `configure_recording_permission` による許可要求・設定画面への導線を表示。設定復帰時と再確認ボタンは `get_server_info {refresh_permissions:true}` を使う。続いて capabilities / diagnostics（ffmpeg・データルートなど）、`rebuild_catalog`、MCP 外のプロセス操作（サーバー再起動 / ログを開く / アップデート確認）を表示する。
- **ジョブ**: ウィンドウが開始したジョブ + 一覧の `active_job` を追跡し、ツールバーにバッジ表示。ポップオーバーから `cancel_job`。
- **エラー表示**: 契約の error_envelope を code + message のアラートで表示。`busy` だけは非モーダルなトーストにする。

純粋ロジック（契約レスポンスのモデル `ContractModels`、表示整形 `NarumiFormat` / `MeetingRowPresentation`、markdown 分割 `MarkdownParser`）は `NarumiMenuBarCore` にあり、`ContractModelsTests`（`contracts/tools/*.json` の examples.output をそのままデコード）・`FormattingTests`・`MarkdownBlocksTests` で検証する。

### AI 接続の範囲と状態管理

接続操作の手順は [README の AI プロバイダ接続](../README.md#ai-プロバイダの接続) を参照する。`ProviderSettingsStore` は設定の revision を使い、別クライアントとの更新競合を表示して再読み込みを求める。API キーは省略なら維持、明示削除なら解除する。入力は保存成功・失敗・画面を閉じる際に消去し、秘密入力を含む保存を通信断後に自動再送しない。

API 方式の認証確認は保存済みキーを使うメタデータ照会。Codex は「ChatGPT でログイン」で公式 App Server の `chatgptDeviceCode` 認証を始める。「確認コード」を表示し、「ブラウザで続ける」で [OpenAI のデバイスログイン画面](https://auth.openai.com/codex/device) を開く。利用者がコードを入力して承認すると、認証状態を更新する。コピーは「確認コードをコピー」を押した場合だけ行い、クリップボードへ自動で書き込まない。API キーは不要で、他アプリのログイン用コールバックポートも使わない。

サーバーとアプリで検証した URL と確認コードを、開始中の操作に対してだけ一時表示する。アプリ内では一時メモリだけに保持し、完了・取消・失敗・画面を閉じる・結果不明・接続や操作の不一致で消去する。アカウントや組織でデバイスログインが無効な場合は明示的に失敗し、ブラウザ OAuth や API キー方式へ自動変更しない。

ログイン・接続確認・候補更新では会議データを送らない。応答が失われた場合は新しい認証操作を始めず、元の `operation_id` / `start_request_id` から状態を確認する。

実行環境の準備は `provider_setup` ジョブで追跡し、取消と状態確認を使う。Codex は公式 npm 版 CLI **0.150.1** の既存インストールから narumi 専用 runtime へコピーし、版・SHA256 を検証する。対応インストールがない場合は準備不可で、Codex.app 内の版不明の実行ファイルは採用しない。ほかのプロバイダは既存 Python 依存の検査・検査結果の保存のみ。グローバルツールの追加、SDK・モデルのダウンロード、利用者指定コマンドの実行は提供しない。

候補一覧は画面表示時にはキャッシュだけを読み、「接続先から候補を更新」で明示的に通信する。Codex の候補は会議設定・プロファイルで選択する。Claude Agent SDK の生成、OpenAI API・音声認識 API、全工程のモデル選択、複数案の生成・統合は未対応。接続確認・候補取得の成功を、実生成の成功とは扱わない。

### Codex の議事録用設定

利用手順は [README の Codex 議事録生成](../README.md#codex-でテキスト議事録を生成)、契約と処理境界は [Codex 議事録生成の設計](../docs/superpowers/specs/2026-08-29-codex-minutes-design.md) を参照する。

`ProcessingConfigurationFields` が会議設定とプロファイルで同じ `MinutesModelSelectionView` を使う。`MinutesModelForm` は `minutes_model` の接続 ID・接続 revision・モデル ID・推論量・試行番号を保持する。`MinutesModelCatalogStore` は保存済みの候補を読み、「モデル候補を取得・更新」で明示的に更新する。モデル一覧で確認できた推論量だけを選択し、空欄は「モデルの既定値」として解決する。接続 revision が変わった場合は「変更後の接続を選び直す」からモデルも再選択する。

`minutes_model` の省略は保持、null は解除、オブジェクトは全体置換。「従来設定」を選んで保存すると Codex の議事録用選択を解除する。`llm_provider` は発話統合・Vision など従来の工程に残し、Codex のモデルを共用しない。`local_only` を自動で変更せず、`subscription_ok` / `api_ok` を明示的に選ぶ。どちらでも Codex は ChatGPT の利用枠を使い、API キー方式へ切り替えない。

`MinutesGenerationDisclosure` は送信先・利用枠・モデル設定と、文字起こし・話者名・会議名・コンテキストのテキストを送ることを表示する。同じ確認済み設定全体を `expected_config` に入れて `regenerate`、または再生成付き `register_context` を呼ぶ。サーバーは会議ロック内で比較し、設定が変わっていれば変更・ジョブ作成・外部送信前に拒否する。保存やモデル選択だけでは会議を送信しない。

Codex は `force=true` を禁止し、同じ入力・選択・実行環境では保存済みの結果を再利用する。送信後の結果が不明なら、narumi は生成を自動で開始し直さない。「新しく生成を試す…」で利用枠の重複消費の可能性を確認し、`cache_epoch` を増やして保存後に再生成する。モデルだけの変更で文字起こし・発話統合をやり直さず、過去の議事録版を保持する。

生成入力はテキストだけとし、音声・動画・画像を Codex に渡さない。固定版の設定でモデルへ渡す tools を空にし、会話履歴を保存しない構成を使う。認証・モデル照会・生成に必要な通信は行う。管理設定や未確認の実効設定を検出した場合は拒否し、任意の管理環境での安全性を保証しない。

Codex の固定 provider では要求・ストリームの再試行を 0 にし、WebSocket を無効にする。ただし SDK 内の HTTP リダイレクト、401 応答後の認証回復、未処理が確定した HTTP/2 NACK の再送は残る。narumi が結果不明の生成を自動で開始し直さないことと、SDK 内部の通信を区別する。全通信の URL 固定や、要求が必ず一度だけ送られることは保証しない。この制限は、アプリと narumi 常駐サーバー間のリダイレクト禁止とは別の範囲である。

## 起動フロー（narumi.app がサーバーを起動する）

`narumi.app` を開くと、メニューバー UI 自身が `narumi-server` のプロセスを起動し、終了時に停止する。Terminal は不要。データ操作はすべて MCP ツール呼び出しで、アプリは会議バンドル・カタログ・録画ファイルに直接触れない。プロセス管理と認証付き接続の準備を `ServerLauncher`（AppKit 側）と `NarumiMenuBarCore` が担当する。

1. **既存サーバーの検出**: repo モードまたは明示的な `NARUMI_SERVER_URL` がある場合は、同じデータルートの信頼できる起動情報を持つ常駐サーバーへ接続できる。そのプロセスはアプリの終了時にも停止しない。通常の bundled 起動では未所有の既存サーバーを採用せず、ポート競合として表示する。HTTP の応答だけで相手を採用せず、起動情報・証明書・認証の不整合はエラーにする。
2. **リポジトリの解決**（repo モード時。優先順）: 環境変数 `NARUMI_REPO` → `UserDefaults` の `narumi.repoPath` → `<repo>/dist/narumi.app` の配置からの推定 → 未設定。repo モードでは `pyproject.toml` と `server/pyproject.toml` が必要。配布版のモード選択は後述の判定順に従い、保存した repoPath より同梱 runtime を優先する。
3. **起動コマンド（repo）**: `/bin/zsh -lc` から `uv run --project <repo> narumi-server --http --host 127.0.0.1 --port <port> --data-root <data-root>` を起動する。リポジトリ・ポート・recorder・データルート・Keychain ヘルパーはシェル文字列に埋め込まず位置パラメータで渡す。ログインシェルで利用者の PATH を読み込んだ後にデータルートとヘルパーを固定し、シェル設定による接続先のずれを防ぐ。同梱 recorder があるときは `--recorder` を追加する。カレントディレクトリはリポジトリ、stdin は `/dev/null`。
4. **ポート・起動情報・認証**: ポートは `NARUMI_SERVER_PORT`（既定 8765）。URL は明示的な `NARUMI_SERVER_URL` または `https://127.0.0.1:<port>/mcp` で、URL だけが設定されていればそのポートを使う。server はデータルートの `runtime/server/` に所有者専用の証明書・秘密鍵・`bootstrap.json` を用意し、認証トークンを Keychain へ保存する。アプリは起動情報の所有者・権限・通常ファイルであることと証明書を検証してから接続する。`--http` というフラグ名は維持するが、未認証の平文 HTTP ではない。
5. **起動待ち**: 1 秒ごとに認証付き接続で `get_server_info` を最大 30 秒試し、サーバー識別情報・対応する契約版・TLS 必須のメタデータを確認したら「稼働中 v… 契約 …」。bundled モードは版・契約 3.0.0・実体パスも厳密に照合する。失敗時は自分が起動したサーバーを停止し、未確定の runtime 切替を復元する。repo モードは、タイムアウト時も生きているプロセスを残し、終了または再起動で停止する。起動直後にプロセスが終了した場合も「起動失敗（ログ参照）」、稼働後に終了すると「停止 (exit N)」。

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
- **サーバー起動（bundled）**: `<venv>/bin/python3 -I -m narumi_server.cli --http --host 127.0.0.1 --port <port> --data-root <data-root> --recorder <.app>/Contents/MacOS/narumi-recorder`。環境変数 `NARUMI_CONTRACTS_DIR` に同梱 contracts、`NARUMI_KEYCHAIN_HELPER` に同じアプリのヘルパーの絶対パスを渡す。Python の環境上書きは除外し、旧 checkout や user site を import しない。データルートはアプリと同じ値に固定する。uv とサーバーは、固定の `/bin/sh` gate で所有情報の保存を待ってから絶対パスで起動する（ログインシェルや PATH 探索は使わない）。異常終了後も前回のプロセスが生きていれば、勝手に停止したり venv を置換したりせず、再同期を止める。

## 自動更新（Sparkle）

- `NarumiMenuBar` のみが SwiftPM の [Sparkle](https://github.com/sparkle-project/Sparkle)（2.9.6 以上、binaryTarget）に依存する。`AppDelegate` が `DesktopUpdater` を保持し、その内部の `SPUStandardUpdaterController` に更新確認を渡す。定期チェックは Sparkle のスケジューラに任せ、確認・適用・終了の各段階でアプリの busy 状態を検査する（`Info.plist` の `SUEnableAutomaticChecks` / `SUScheduledCheckInterval` は `scripts/build-app.sh` が書く）。
- フィード: 通常は `Info.plist` の `SUFeedURL`。環境変数 `NARUMI_SPARKLE_FEED_URL` があれば delegate の `feedURLString(for:)` がそれを返す（ローカル更新 E2E 用。本番では設定しない）。
- 録画・開始停止・起動準備・既知ジョブの処理中は更新確認と適用を延期する。適用時は通常の終了経路で管理中 server を停止する。新バージョンの初回起動では manifest 差分により venv を再同期する。
- ジョブの保護対象はアプリが開始した、または一覧から把握した処理。別の CLI / MCP クライアントからのジョブ開始と更新の原子的な排他は未対応なので、その処理中に更新しない。
- 鍵: 更新の署名検証は `Info.plist` の `SUPublicEDKey`（`app/sparkle-public-key.txt` から埋め込む）。Keychain の `jp.btajp.narumi` account を使用する。`generate_keys --account jp.btajp.narumi -x <file>` でリポジトリ外へバックアップし、ファイルを所有者だけが読める状態にする。秘密鍵や Apple 資格情報はリポジトリ・ログに置かない。
