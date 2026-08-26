# narumi app（Swift Package）

録画 MVP（Step 1）の Swift 側。3 つのターゲットからなる。

| ターゲット | 種別 | 役割 |
|---|---|---|
| `NarumiRecorderKit` | ライブラリ | ScreenCaptureKit キャプチャ → AVAssetWriter で **別ファイル** 書き出し。イベント型・引数解析・ディスプレイ選択などの純粋ロジックはこの中の SCK 非依存な型に置き、`swift test` で検証する |
| `narumi-recorder` | CLI | server がサブプロセスとして起動する録画ヘルパー。stdout に JSON Lines でイベントを出す |
| `NarumiMenuBar` | メニューバーアプリ `narumi.app` | MCP クライアント。server の公開ツール（`start_recording` / `stop_recording` / `get_server_info`）を呼ぶだけで、ファイルや recorder には触れない（AGENTS.md 絶対原則 3） |

## ビルドとテスト

```sh
cd app
swift build                 # debug
swift build -c release      # app/.build/release/narumi-recorder を生成（server が探すパス）
swift test                  # XCTest（下記の注意を参照）
scripts/build-app.sh        # リポジトリ直下から。dist/narumi.app を組み立てて ad-hoc 署名
```

- 要件: macOS 15 以降、Swift 6.0 以降のツールチェーン。外部パッケージ依存なし（引数解析は自前）。
- **`swift test` には Xcode が必要**。`xcode-select -p` が `/Library/Developer/CommandLineTools` を指している環境では XCTest が無く `no such module 'XCTest'` になる。切り替えずに実行するには `DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer swift test`。ビルド（`swift build`）は Command Line Tools だけで通る。
- テストは ScreenCaptureKit・TCC・ネットワークに依存しない（イベント JSON の厳密一致、引数解析、ディスプレイ選択、ファイル名、`recorder.json` の書き出し）。

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
- **`.app`（`dist/narumi.app`）**: `Info.plist` に `NSMicrophoneUsageDescription` / `NSScreenCaptureUsageDescription` を持ち、`jp.btajp.narumi` として許可が記録される。同梱の `Contents/MacOS/narumi-recorder` をこの .app から起動する構成にすると許可が .app に紐づく。ただし現状のメニューバー UI は recorder を直接起動しない（server が起動する）ため、.app の許可は UI 自身の動作にのみ効く。
- ad-hoc 署名（`codesign --sign -`）はビルドのたびに署名が変わるため、TCC の許可が再度求められることがある。

## server から recorder を見つける方法

server の `RecordingController` は次の順で実行ファイルを探す。

1. 環境変数 `NARUMI_RECORDER`（絶対パス）
2. `app/.build/release/narumi-recorder`
3. `app/.build/debug/narumi-recorder`

いずれも無ければ `recorder_unavailable` エラー。開発時は `cd app && swift build -c release` を一度実行しておけばよい。`.app` 版を使う場合は `NARUMI_RECORDER=/path/to/dist/narumi.app/Contents/MacOS/narumi-recorder` を指定する。

## メニューバーアプリ（NarumiMenuBar）

- `NSStatusItem`: 待機中 🕵️ / 録画中 ⏺。メニューは「録画開始…」「録画停止」「サーバー: <状態>」「終了」。
- 「録画開始…」は `NSAlert` で会議名を聞き、`start_recording {meeting_name, request_id}` を呼ぶ。「録画停止」は `stop_recording {request_id[, meeting_id]}`。`request_id` は UUID を毎回発番する。
- 「サーバー: …」は 5 秒ごとに `get_server_info` を呼んで更新する（`version` / `contract_version` があれば表示。応答に `recording` があればアイコンにも反映）。
- 接続先は環境変数 `NARUMI_SERVER_URL`（既定 `http://127.0.0.1:8765/mcp`）。`MCPClient` は `initialize`（protocolVersion `2025-06-18`）→ `notifications/initialized` → `tools/call` を JSON-RPC 2.0 の POST で行い、`Mcp-Session-Id` を保持して送り返す。応答は `application/json` と `text/event-stream`（`data:` 行から同じ id の応答を取り出す）の両方を受け付ける。
- ツールエラー（`isError` / 構造化 `{"error":{code,message}}`）は `NSAlert` で表示する。
