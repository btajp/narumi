# narumi

narumi は、macOS 上でローカルに会議を録画し、終了後に議事録を自動生成するシステムです。メニューバーのワンボタンで画面・マイク・システム音声を **別トラック** で録画し、ffmpeg による分離 → 文字起こし → 話者分離 → 突合 → 生成 → エクスポートの工程を、会議ごとの「セッションバンドル」（ファイルが正本）に積み上げます。決定的な処理はスクリプトで固定手順として実行し、LLM は読解・統合・生成にだけ使います。既定はローカル完結（Whisper 系）で、音声やテキストを外部へ送るエンジン / LLM は会議ごとの `external_send_policy` で明示的にオプトインしない限り使えません。

相棒の **gaia-library**（記憶の索引 MCP）とは任意連携です。gaia-library が無くても narumi 単体で完結し、あれば語彙・参加者・前回要点などのコンテキスト注入で精度が上がります（コンテキスト注入と gaia-library エクスポートは Step 3 以降で **未実装** です）。設計の正本は Notion「議事録生成システム」ページで、リポジトリ内の要約と開発ルールは [AGENTS.md](AGENTS.md)、基盤の具体設計は [docs/superpowers/specs/](docs/superpowers/specs/) にあります。

## 動作要件

- macOS 15 以降（マイク取り込みに ScreenCaptureKit の `captureMicrophone` を使うため）
- [uv](https://docs.astral.sh/uv/)（Python 3.12 以降を管理。`.venv` はリポジトリ直下に作られます）
- ffmpeg / ffprobe（`brew install ffmpeg`）
- Xcode Command Line Tools（`xcode-select --install`。録画アプリのビルドに Swift 6.3 相当のツールチェーンが必要）。`swift test` だけは XCTest を含む Xcode 本体が必要で、`xcode-select` が CLT を指す環境では `DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer swift test` で実行する（`swift build` は CLT だけで通る）
- 任意: gaia-library（無くてもテストは全て通ります）

## セットアップ

```sh
git clone <this repo> narumi && cd narumi
uv sync                      # Python 依存（pipeline + server。dev グループに軽量 extras 込み）
uv run narumi-dev doctor     # ffmpeg / recorder / エンジンの状態を確認
cd app && swift build -c release && cd ..   # 録画ヘルパー narumi-recorder（app/ が揃ってから）
```

`uv run narumi-dev doctor` は ffmpeg / ffprobe が見つからないと終了コード 1 で失敗します。録画ヘルパーは `NARUMI_RECORDER` 環境変数、無ければ `app/.build/{release,debug}/narumi-recorder` の順で探します。

## デスクトップアプリの使い方

```sh
scripts/build-app.sh     # app/ を release ビルドし、dist/narumi.app を組み立てて ad-hoc 署名
open dist/narumi.app     # メニューバーに 🕵️ が出る。「録画開始…」で録画
```

`narumi.app` は起動時に `narumi-server`（Streamable HTTP、`http://127.0.0.1:8765/mcp`）を自分で起動し、終了時に停止します。Terminal でサーバーを起動しておく必要はありません。ただしこれは統合計画の**選択肢 1**（アプリがリポジトリの `uv run narumi-server` を起動する）であり、自己完結した配布物ではありません。動かすマシンには **uv と、このリポジトリのチェックアウト（`uv sync` 済み）** が引き続き必要です。

- リポジトリは `NARUMI_REPO` → 「リポジトリを選択…」で保存した設定 → `.app` が `<repo>/dist/narumi.app` にあるならそのリポジトリ、の順で決まります。`scripts/build-app.sh` の出力をそのまま `open` する限り追加設定は要りません。
- `scripts/dev.sh` などで先にサーバーを起動しておくと、アプリはそれに接続するだけで自分では起動も停止もしません。
- ポートは `NARUMI_SERVER_PORT`、データルートは `NARUMI_HOME` で変えられます。ログは `~/Library/Logs/narumi/server.log`（メニューの「ログを開く」）。
- 詳細（起動コマンド、状態表示、終了時の録画確定）は [app/README.md](app/README.md) の「起動フロー」を参照してください。

## パイプライン概要

```
録画（screen.mp4 / mic.m4a / system.m4a を別トラック）
  → preprocess   ffmpeg で 16kHz mono wav に分離（決定的）
  → transcribe   文字起こし（エンジン抽象化。既定はローカル Whisper: mlx-whisper → faster-whisper）
  → diarize      話者分離 第 1 層 = トラック（me / other）。第 2 層 pyannote は抽象化のみ（既定 none）
  → align        第 1 段: タイムスタンプ＋テキストアンカーの決定的アライメント（区間対応表）
  → integrate    第 2 段: 区間ごとの LLM 統合（単一系統なら素通し）
  → generate     議事録 minutes/vN/（版は増えるだけ。上書きしない）
  → export       プラグイン型エクスポート（markdown / html）
```

各工程は `Bundle.run_stage(key, inputs, params, ...)` で実行され、`manifest.json` に入力ハッシュと生成パラメータを記録します。同じ入力＋同じパラメータなら再実行はスキップされ、「同じ入力 → 同じ版」が守られます。dev CLI の `regenerate` は align 以降だけを再実行します。MCP ツールの `regenerate` は、未処理・失敗した決定的工程（前処理・文字起こし・話者分離）と設定変更で params が変わった工程だけを冪等に実行してから align 以降を再実行します（`force` でも前処理・文字起こしは強制しません）。

### 実装状況

| 項目 | 状態 |
|---|---|
| セッションバンドル / manifest / 冪等ステージ実行 | 実装済み |
| 契約（`contracts/`）と MCP サーバー（v1 の 24 ツール、stdio / Streamable HTTP） | 実装済み |
| 操作面パリティ拡張（録画状態・議事録取得・全文検索・既存録画の取り込み・プロファイル＋自動エクスポート・トラック破棄・会議削除・ジョブ取消・カタログ再構築） | 実装済み（MCP ツール＋CLI。アプリ本体ウィンドウは未実装） |
| 製品 CLI `narumi`（契約から自動生成の 1:1 写像。サーバー自動検出、無ければ in-process） | 実装済み |
| 録画アプリ / `narumi-recorder` | 実装済み（ScreenCaptureKit の実キャプチャ経路は実機での手動確認が未了） |
| `narumi.app` によるサーバーの起動・停止（Terminal 不要） | 実装済み（uv とリポジトリのチェックアウトが必要。自己完結配布は未対応） |
| ffmpeg 分離 → Whisper → プレーン議事録（`narumi.pipeline`） | 実装済み（`fake` エンジンで E2E テスト済み。実エンジンは `-m real` の opt-in smoke） |
| 外部トランスクリプト突合（Notion AI / Zoom / Meet） | 未実装（`register_context` は原文保存のみ） |
| キースライド抽出（pHash）/ Vision 読解 / 第 3 層話者判定 | 未実装 |
| gaia-library 照会によるコンテキスト注入 | 未実装 |
| Notion / gaia-library エクスポーター | 未実装 |
| アップグレード再生成（影響区間だけ第 2 段を再実行） | 未実装（拡張点のみ確保） |

## コマンド

```sh
uv sync                                                  # 初回セットアップ
uv run pytest                                            # Python 全テスト（pipeline + server）
uv run ruff check . && uv run ruff format --check .      # Lint / フォーマット
uv run narumi --help                                     # 製品 CLI（契約から自動生成。サーバー自動検出）
uv run narumi-dev --help                                 # dev CLI（ライブラリ直叩き。デバッグ用）
uv run narumi-server --stdio                             # MCP サーバー（stdio dev モード）
uv run narumi-server --http --port 8765                  # MCP サーバー（Streamable HTTP 常駐）
scripts/dev.sh                                           # HTTP サーバー起動（GAIA_LIBRARY_CMD があれば gaia-library も）
scripts/build-app.sh && open dist/narumi.app             # メニューバーアプリ（サーバーはアプリが起動する）
scripts/gen-types.sh                                     # 契約 → pydantic 型生成（生成物はコミットしない）
cd app && swift build && swift test                      # 録画アプリ / recorder CLI
```

### 製品 CLI `narumi`

`narumi` は MCP ツールの **1:1 写像** です（`docs/superpowers/specs/2026-08-27-narumi-surface-parity-design.md`）。サブコマンドは `contracts/` から自動生成され（ツール名の `_` を `-` に置換）、オプションは `inputSchema` から型付きで生成されます（string / integer / number / boolean はそのまま、array / object は JSON 文字列、`scope` セレクタは名前 1 つか JSON 配列）。`request_id` は省略すると UUID4 を自動発番します。

```sh
uv run narumi list-meetings --scope cloudnative --limit 10
uv run narumi search-transcripts --query "オンボーディング"
uv run narumi get-minutes --meeting-id <meeting_id> [--version N]
uv run narumi import-recording --meeting-name "定例" --mic-path /abs/mic.m4a --system-path /abs/system.m4a
uv run narumi delete-meeting --meeting-id <meeting_id> --confirm
uv run narumi tool <tool_name> --json '{"...": "..."}'   # 汎用エスケープハッチ（任意のツールを生 JSON で）
```

接続先は `--server-url`（既定 `NARUMI_SERVER_URL` → `http://127.0.0.1:8765/mcp`）です。サーバーが応答すればそこへ MCP（Streamable HTTP）で送り、応答が無ければ同じディスパッチ経路を in-process で実行します（`--require-server` / `--in-process` で強制。録画系ツールは in-process では拒否）。出力は結果 JSON（`--pretty` 既定、`--raw` で 1 行）で、エラーは契約の `error_envelope` を stderr に出し終了コード 2 で終わります。`--data-root PATH`（または `NARUMI_HOME`）は in-process 実行のデータルートを切り替えます。

### dev CLI `narumi-dev`

`narumi-dev` はライブラリを直接呼ぶ **開発者向け** デバッグツールで、製品の操作面には数えません。UI = API パリティの原則（AGENTS.md 絶対原則 3）が適用されるのはアプリと製品 CLI で、どちらも MCP のツール面だけを使います。

```sh
uv run narumi-dev import-recording --name "定例" --mic mic.m4a --system system.m4a [--screen screen.mp4] \
    [--scope cloudnative] [--engagement acme] [--started-at 2026-08-27T12:00:00+09:00] [--copy|--link]
uv run narumi-dev show <meeting_id>                      # manifest の要約を JSON で表示
uv run narumi-dev config <meeting_id> --transcription-engine fake --external-send-policy local_only
uv run narumi-dev process <meeting_id> [--force]         # 全工程を実行（stages / skipped / minutes_version を JSON 出力）
uv run narumi-dev regenerate <meeting_id> [--force] [--reason TEXT]
uv run narumi-dev export <meeting_id> --to markdown [--path out.md] [--version N]
uv run narumi-dev list [--scope NAME ...] [--query TEXT] [--limit N]
uv run narumi-dev catalog rebuild                        # narumi.db をバンドルから全再構築
uv run narumi-dev doctor                                 # 環境診断
```

`--data-root PATH`（または `NARUMI_HOME`）でデータルートを切り替えられます。どちらの CLI も `narumi.errors.NarumiError` は `{"error": {"code", "message", "details"}}` の JSON を stderr に出し、終了コード 2 で終わります。

## データの置き場所

データルートは `NARUMI_HOME`（既定 `~/Library/Application Support/narumi`）です。

```
$NARUMI_HOME/
├── narumi.db                 # 再構築可能なカタログ＋検索索引（`narumi-dev catalog rebuild`）
└── meetings/<meeting_id>/    # セッションバンドル（ファイルが正本）
    ├── manifest.json
    ├── tracks/               # 原録音・録画（破棄可）
    ├── preprocess/           # ffmpeg 派生物（再生成可）
    ├── transcripts/  diarization/  merged/
    ├── minutes/v1/ …         # 版は増えるだけ
    ├── context/              # 登録されたコンテキスト原文
    └── logs/
```

`narumi.db` が壊れてもバンドルから全再構築できます。逆にバンドルを消すとその会議は失われます。

## MCP クライアント設定例

サーバー名は `narumi`、HTTP のエンドポイントは `http://127.0.0.1:8765/mcp` です。stdio では `uv run narumi-server --stdio` をリポジトリのディレクトリで起動する必要があるため、`uv --directory <repo>` を使います。

### Claude Code

```sh
# stdio
claude mcp add --transport stdio narumi -- uv --directory /path/to/narumi run narumi-server --stdio
# Streamable HTTP（narumi.app を起動しておく、または scripts/dev.sh で常駐させておく）
claude mcp add --transport http narumi http://127.0.0.1:8765/mcp
```

`.mcp.json` に書く場合:

```json
{
  "mcpServers": {
    "narumi": {
      "command": "uv",
      "args": ["--directory", "/path/to/narumi", "run", "narumi-server", "--stdio"]
    }
  }
}
```

### Codex

`~/.codex/config.toml`:

```toml
[mcp_servers.narumi]
command = "uv"
args = ["--directory", "/path/to/narumi", "run", "narumi-server", "--stdio"]

# Streamable HTTP を使う場合（リモート MCP 対応版の Codex CLI）
# [mcp_servers.narumi]
# url = "http://127.0.0.1:8765/mcp"
```

## 開発ルール

- 契約（`contracts/`）が正本です。変更は契約 → `uv run pytest pipeline/tests/contracts` → 実装の順で行います。
- テストは実エンジン / 実プロバイダ / 実録画に依存せず、`fake` 実装と ffmpeg の合成音声で通します。実エンジンの smoke テストは `-m real` で opt-in です。
- コミットは Conventional Commits（feat / fix / refactor / test / docs / chore）。

詳細は [AGENTS.md](AGENTS.md) と [docs/superpowers/specs/2026-08-27-narumi-foundation-design.md](docs/superpowers/specs/2026-08-27-narumi-foundation-design.md) を参照してください。
