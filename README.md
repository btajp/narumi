# narumi

narumi は、macOS 上でローカルに会議を録画し、終了後に議事録を自動生成するシステムです。アプリから画面・マイク・システム音声を **別トラック** で録画し、ffmpeg による分離 → 文字起こし → 話者分離 → 突合 → 生成 → エクスポートの工程を、会議ごとの「セッションバンドル」（ファイルが正本）に積み上げます。決定的な処理はスクリプトで固定手順として実行し、LLM は読解・統合・生成にだけ使います。既定はローカル完結（Whisper 系）で、音声やテキストを外部へ送るエンジン / LLM は会議ごとの `external_send_policy` で明示的にオプトインしない限り使えません。

相棒の **gaia-library**（記憶の索引 MCP）とは任意連携です。gaia-library が無くても narumi 単体で完結し、接続すると語彙・参加者・前回要点などを会議ブリーフに取り込みます。議事録の書き戻しは提案キューへの登録だけで、人間の承認は gaia-library 側で行います。設計の正本は Notion「議事録生成システム」ページで、リポジトリ内の要約と開発ルールは [AGENTS.md](AGENTS.md)、基盤の具体設計は [docs/superpowers/specs/](docs/superpowers/specs/) にあります。

## インストールと録画

配布版は Apple Silicon（arm64）・macOS 15 以降に対応します。

1. [GitHub Releases](https://github.com/btajp/narumi/releases/latest) の ZIP を展開し、`narumi.app` を「アプリケーション」へ移します。
2. `narumi.app` を開きます。初回は同梱ランタイムの準備が終わるまで待ちます。Python や uv、ソースコードの別途導入は不要ですが、初回の依存・モデル取得にはネットワークが必要です。
3. メイン画面の「録画開始」を押し、会議名を入力します。プロファイルと scope は空欄なら既定設定を使います。macOS が画面収録・マイクの許可を求めた場合は設定します。
4. 録画中は同じ画面に経過時間と「録画停止」が表示されます。終了後は会議一覧から結果を開けます。

ffmpeg / ffprobe は別途必要です。アプリの「診断」で検出状態を確認できます。更新は GitHub Releases を自動確認し、利用者の操作で適用します。録画中やアプリが把握している処理の実行中は更新を延期します。別の CLI / MCP クライアントから処理している間は更新しないでください。会議データはアプリ本体とは別の Application Support 配下に保存するため、通常の更新で削除されません。

## 開発環境の要件

- macOS 15 以降（マイク取り込みに ScreenCaptureKit の `captureMicrophone` を使うため）
- [uv](https://docs.astral.sh/uv/)（Python 3.12 以降を管理。`.venv` はリポジトリ直下に作られます）
- ffmpeg / ffprobe（`brew install ffmpeg`）
- Xcode Command Line Tools（`xcode-select --install`。録画アプリのビルドに Swift 6.3 相当のツールチェーンが必要）。`swift test` だけは XCTest を含む Xcode 本体が必要で、`xcode-select` が CLT を指す環境では `DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer swift test` で実行する（`swift build` は CLT だけで通る）
- 任意: gaia-library（無くてもテストは全て通ります）

## 開発環境のセットアップ

```sh
git clone <this repo> narumi && cd narumi
uv sync                      # Python 依存（pipeline + server。dev グループに軽量 extras 込み）
uv run narumi-dev doctor     # ffmpeg / recorder / エンジンの状態を確認
cd app && swift build -c release && cd ..   # 録画ヘルパー narumi-recorder（app/ が揃ってから）
```

`uv run narumi-dev doctor` は ffmpeg / ffprobe が見つからないと終了コード 1 で失敗します。録画ヘルパーは `NARUMI_RECORDER` 環境変数、無ければ `app/.build/{release,debug}/narumi-recorder` の順で探します。

## 開発用デスクトップビルド

```sh
scripts/build-app.sh     # app/ を release ビルドし、dist/narumi.app を組み立てて ad-hoc 署名
open dist/narumi.app     # メイン画面と、名前付きのメニューバー項目を表示
```

`narumi.app` は起動時に `narumi-server`（Streamable HTTP、`http://127.0.0.1:8765/mcp`）を自分で起動し、終了時に停止します。Terminal でサーバーを起動しておく必要はありません。起動方法は 2 モードあります（詳細は [app/README.md](app/README.md) の「ランタイムモード」）。

- **repo モード**（開発用。上の `scripts/build-app.sh` のとおりオプション無しでビルドした場合）: アプリがリポジトリの `uv run narumi-server` を起動します。動かすマシンには **uv と、このリポジトリのチェックアウト（`uv sync` 済み）** が必要です。
- **bundled モード**（配布用。`scripts/build-app.sh --runtime` でビルドした場合）: `.app` が同梱の uv・wheels・requirements から `NARUMI_HOME/runtime/` に venv を自分で作って起動します。**リポジトリのチェックアウトも uv のインストールも不要**です（初回はネットワーク必須。後述「配布」参照）。

- `NARUMI_RUNTIME_MODE=repo|bundled` と明示的な `NARUMI_REPO` 指定を除き、同梱ランタイムがあれば bundled モードを優先します。以前保存したリポジトリ設定だけでは配布版を開発モードへ切り替えません。
- 開発用 repo モード、または `NARUMI_SERVER_URL` で明示した外部サーバーには接続できます。通常の bundled 起動で既存サーバーがポートを使用している場合は、誤接続を避けるため競合として表示します。
- ポートは `NARUMI_SERVER_PORT`、データルートは `NARUMI_HOME` で変えられます。ログは `~/Library/Logs/narumi/server.log`（メニューの「ログを開く」）。
- 詳細（起動コマンド、状態表示、終了時の録画確定）は [app/README.md](app/README.md) の「起動フロー」を参照してください。

## パイプライン概要

```
録画（screen.mp4 / mic.m4a / system.m4a を別トラック）
  → preprocess   ffmpeg で 16kHz mono wav に分離（決定的）
  → brief        会議ブリーフ context/brief.json（Gaia 接続設定がある場合だけ照会。語彙は transcribe / integrate、本文は minutes プロンプトへ）
  → transcribe   文字起こし（エンジン抽象化。既定はローカル Whisper: mlx-whisper → faster-whisper）
  → diarize      話者分離 第 1 層 = トラック（me / other）、第 2 層 pyannote（既定 none）、第 4 層 = 外部トランスクリプトの話者名
  → slides       キースライド抽出（画面トラックがあるときだけ。フレーム抽出 → pHash 重複除去 → preprocess/slides.json）
  → align        第 1 段: タイムスタンプ＋テキストアンカーの決定的アライメント（own / ext 全系統の区間対応表）
  → layer3       第 3 層 = 画面の話者ハイライト読解（vision 対応プロバイダが送信ポリシーで許可されたときだけ）
  → integrate    第 2 段: 区間ごとの LLM 統合（単一系統なら素通し。merged/integrate_cache.json で影響区間だけ再実行）
  → generate     議事録 minutes/vN/（版は増えるだけ。上書きしない。キースライド画像とブリーフを埋め込む）
  → export       プラグイン型エクスポート（markdown / html / notion / gaia-library）
```

各工程は `Bundle.run_stage(key, inputs, params, ...)` で実行され、`manifest.json` に入力ハッシュと生成パラメータを記録します。同じ入力＋同じパラメータなら再実行はスキップされ、「同じ入力 → 同じ版」が守られます。dev CLI の `regenerate` は align → integrate → generate だけを再実行します。MCP ツールの `regenerate` は、未処理・失敗した上流工程（前処理・ブリーフ・文字起こし・話者分離・スライド抽出）と、設定変更や `register_context` で inputs / params が変わった工程だけを冪等に実行してから align 以降を再実行します（`force` でも上流工程は強制しません）。

### 実装状況

| 項目 | 状態 |
|---|---|
| セッションバンドル / manifest / 冪等ステージ実行 | 実装済み |
| 契約（`contracts/`）と MCP サーバー（v1 の 27 ツール、stdio / Streamable HTTP） | 実装済み |
| 操作面パリティ拡張（録画状態・議事録取得・全文検索・既存録画の取り込み・プロファイル＋自動エクスポート・トラック破棄・会議削除・ジョブ取消・カタログ再構築） | 実装済み（MCP ツール＋CLI＋アプリ本体ウィンドウ） |
| 製品 CLI `narumi`（契約から自動生成の 1:1 写像。サーバー自動検出、無ければ in-process） | 実装済み |
| 録画アプリ / `narumi-recorder` | 実装済み（メイン画面とメニューバーから同じ MCP 録画操作を利用） |
| `narumi.app` によるサーバーの起動・停止（Terminal 不要） | 実装済み（repo モード＝uv とチェックアウトが必要 / bundled モード＝`--runtime` ビルドで自己完結） |
| ランタイム同梱 .app と Sparkle 自動更新 | 実装済み（Developer ID 署名・公証・公開物照合を行う配布手順。ffmpeg / ffprobe は別途必要） |
| ffmpeg 分離 → Whisper → プレーン議事録（`narumi.pipeline`） | 実装済み（`fake` エンジンで E2E テスト済み。実エンジンは `-m real` の opt-in smoke） |
| 外部トランスクリプト突合（Notion AI / Zoom / Meet / Teams） | 実装済み（WebVTT / SRT / Zoom txt / プレーンの決定的パーサ。`register_context` が即時パースして `transcripts/ext-*` として突合に参加、話者名は第 4 層で実名解決。URL は参照保存のみで取得は未実装） |
| キースライド抽出（pHash）/ Vision 読解 / 第 3 層話者判定 | 実装済み（pHash は pillow の自前実装、議事録へ画像埋め込みまで。第 3 層は vision 対応プロバイダが送信ポリシーで許可されたときだけ実行。実 vision プロバイダでの実機確認は未了） |
| gaia-library 照会によるコンテキスト注入（会議ブリーフ） | 実装済み（実契約の scope 付き照会と案件名から ID への解決。参照結果を `context/brief.json` に保存し、語彙・参加者・背景を注入。契約版とクライアントの識別情報も確認） |
| Gaia 接続設定 | 実装済み（アプリの「Gaia 接続」から URL・API キーの保存、無効化、接続テスト。MCP と CLI にも同じ操作を公開） |
| Notion / gaia-library エクスポーター | 実装済み（Notion は REST でページ作成＋Markdown→ブロック変換。スライド画像のアップロードと実環境検証は未対応。gaia-library は `propose_update` の提案キューのみ＝絶対原則 5。実プロセスで認証・scope・提案の重複防止・agent の承認拒否を検証） |
| アップグレード再生成（影響区間だけ第 2 段を再実行） | 実装済み（`merged/integrate_cache.json` の区間フィンガープリントで、追加ソースが触れた区間だけ LLM を再実行） |

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

### gaia-library との接続（任意）

gaia-library の HTTP サーバーを起動し、narumi 用の agent クライアントと API キーを用意しておきます。narumi のメインウィンドウで「Gaia 接続」を開きます。

1. **接続先 URL**: 同じ Mac 上の HTTP エンドポイントを入力します（例 `http://127.0.0.1:4111/mcp`）。外部への誤送信を防ぐため loopback 以外、URL 内の認証情報、クエリ、リダイレクトは拒否します。
2. **API キー**: gaia-library が発行したキーを入力して保存します。キー欄は再表示しません。同じ URL では空欄で既存キーを維持し、URL を変えた場合は旧キーを引き継ぎません。削除・連携無効化は専用の操作を使います。
3. **接続テスト**: 保存後に実行します。サーバーの契約版・クライアント名・既定 scope を確認するだけで、提案や承認は行いません。会議の scope / engagement は既存のプロファイル・会議設定で指定します。

保存先は `<NARUMI_HOME>/gaia.json`（所有者だけが読み書きできる `0600`）です。キーは暗号化せず保存するため、同じ OS アカウントからは読めます。プロファイル、会議バンドル、ツール応答にはキーを保存しません。保存済み設定を優先し、未保存の場合だけ `NARUMI_GAIA_URL` / `NARUMI_GAIA_API_KEY` を参照します。アプリで無効化した後は環境変数から再有効化されません。

未設定なら従来どおり単体で動作します。設定済みの接続が失敗した場合はエラーを表示し、別サーバーや未認証接続へ黙って切り替えません。既存ブリーフを再利用する場合も接続先の識別情報を確認するため、設定した gaia-library サーバーが必要です。

実 gaia-library との結合テストは `NARUMI_GAIA_BIN=/path/to/gaia uv run pytest pipeline/tests/test_gaia_live.py` で任意実行できます。一時設定・DB・キーを作成して検証し、ユーザーの既存データは使いません。環境変数を指定しなければスキップされます。

### 製品 CLI `narumi`

`narumi` は MCP ツールの **1:1 写像** です（`docs/superpowers/specs/2026-08-27-narumi-surface-parity-design.md`）。サブコマンドは `contracts/` から自動生成され（ツール名の `_` を `-` に置換）、オプションは `inputSchema` から型付きで生成されます（string / integer / number / boolean はそのまま、array / object は JSON 文字列、`scope` セレクタは名前 1 つか JSON 配列）。`request_id` は省略すると UUID4 を自動発番します。

nullable な項目は `--clear-<項目>` で JSON `null` を明示できます。例えば `narumi set-gaia-connection --clear-url` は連携を無効化し、`--clear-api-key` はキーだけを削除します。省略は既存値を保持し、文字列 `null` を値として渡しても削除にはなりません。値と clear の同時指定は拒否します。既存オプション名と衝突する場合は clear 側に接尾辞を付けるため、正確な名前は `--help` で確認してください。

```sh
uv run narumi list-meetings --scope cloudnative --limit 10
uv run narumi search-transcripts --query "オンボーディング"
uv run narumi get-minutes --meeting-id <meeting_id> [--version N]
uv run narumi import-recording --meeting-name "定例" --mic-path /abs/mic.m4a --system-path /abs/system.m4a
uv run narumi delete-meeting --meeting-id <meeting_id> --confirm
uv run narumi tool <tool_name> --json '{"...": "..."}'   # 汎用エスケープハッチ（任意のツールを生 JSON で）
```

接続先は `--server-url`（既定 `NARUMI_SERVER_URL` → `http://127.0.0.1:8765/mcp`）です。サーバーが応答すればそこへ MCP（Streamable HTTP）で送り、応答が無ければ同じディスパッチ経路を in-process で実行します（`--require-server` / `--in-process` で強制。録画系ツールは in-process では拒否）。出力は結果 JSON（`--pretty` 既定、`--raw` で 1 行）で、エラーは契約の `error_envelope` を stderr に出し終了コード 2 で終わります。`--data-root PATH`（または `NARUMI_HOME`）は in-process 実行のデータルートを切り替えます。

Gaia のキー保存など、契約に `writeOnly` 入力を持つツールの HTTP 通信は同じ Mac 上の loopback HTTP に限定します。`localhost` は数値アドレスへ固定し、接続確認・初期化から終了まで環境プロキシとリダイレクトを使いません。それ以外のツールの接続方法は変わりません。

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
├── gaia.json                 # 任意の Gaia 接続設定・API キー（0600。会議データとは別）
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

## 配布（ランタイム同梱 .app と自動更新）

設計の正本は [docs/superpowers/specs/2026-08-27-narumi-app-distribution-design.md](docs/superpowers/specs/2026-08-27-narumi-app-distribution-design.md)。対象は Apple Silicon（arm64）・macOS 15 以降、配布経路は GitHub Releases のみ。

### ランタイム同梱 .app のビルド

```sh
scripts/build-app.sh --runtime    # dist/narumi.app（ランタイム同梱、ad-hoc 署名）
```

`--runtime` は `Contents/Resources/runtime/` に uv 単体バイナリ（版と sha256 は `scripts/runtime.lock.json` に固定）・narumi / narumi-server の wheel・ハッシュ付き `requirements.txt`・契約 manifest に列挙された契約ファイル・runtime manifest を同梱します。初回起動と更新時に、必要に応じて `NARUMI_HOME/runtime/` の Python 3.13 と venv を準備します。**リポジトリのチェックアウトも uv の事前インストールも不要**ですが、初回の依存取得にはネットワークが必要で、ffmpeg / ffprobe は別途必要です。スライド処理の依存も含みます。進捗はアプリに表示し、ログは `~/Library/Logs/narumi/runtime.log` に保存します。

版は `VERSION` ファイルが正本（`CFBundleShortVersionString`）、`CFBundleVersion` は `git rev-list --count HEAD` です。Python パッケージ、サーバー、録画ヘルパー、`CHANGELOG.md` の版との一致は `scripts/check-version.sh` で検査します。

### リリース（`scripts/release-app.sh <version>`）

Developer ID 署名 → Apple 公証 → Sparkle フィード生成 → GitHub Release（draft）までを行い、公開前に停止します。

1. clean な `main` と `origin/main` の一致、版、lock、既存リリース、署名鍵を検査します。Sparkle ツールは SwiftPM が取得したものを自動検出します。別の既存配置を使う場合だけ `SPARKLE_BIN` を指定します。
2. 署名には `APPLE_SIGNING_IDENTITY` / `APPLE_API_KEY` / `APPLE_API_ISSUER` / `APPLE_API_KEY_PATH` を使います。ローカル設定 `~/.config/narumi/release.env` があれば読み込みます。設定ファイルの場所を変える場合は `NARUMI_RELEASE_ENV` を指定します。秘密情報をリポジトリやログに記録しないでください。
3. 専用の `dist/release/v<version>/build/narumi.app` へビルドし、同梱 uv を含めて署名します。使用中の `dist/narumi.app` は置き換えません。wheel とアプリの収録ファイルも検査します。
4. 公証・staple 後に最終 ZIP を作り、再展開して署名と公証チケットを検証します。その ZIP から EdDSA 署名と appcast を作り、版・build・公開 URL・長さ・SHA256 を照合します。
5. 出荷元に変更がないことを再検査し、固定したコミット SHA を指定して draft を作成します。アップロードするのは ZIP と appcast のみです。draft を再取得してハッシュまで照合します。
6. 録画・処理がないことを確認し、旧アプリを正常終了します。管理していたプロセスと待受ポートの停止を確認してから、同じ ZIP を `/Applications` にインストールし、起動・同梱サーバー・診断を検証します。`scripts/release-app.sh <version> --verify-draft` が成功してから公開します。
7. 公開後に `scripts/release-app.sh <version> --verify-published` で公開タグ・latest・匿名ダウンロードの内容を照合し、アプリの更新確認も行います。失敗時は公開を取り下げて調査し、公開済み asset を上書きして修正しません。

別の出力先を使う場合は、最初から最後まで同じ `RELEASE_DIR` を指定します。初回出荷での「最新です」という表示は、旧版から新版への更新適用を検証したことにはなりません。

### Sparkle 鍵の管理（重要）

- 秘密鍵は初回に一度だけ `generate_keys --account jp.btajp.narumi` で作り、ログイン Keychain に保存します。リリース処理が暗黙に鍵を作ることはありません。
- 公開鍵は `app/sparkle-public-key.txt` にコミットし、`Info.plist` の `SUPublicEDKey` として全ビルドに埋め込まれます。
- `generate_keys --account jp.btajp.narumi -x <退避先>` で秘密鍵を安全な場所にバックアップしてください。読み取り権限を所有者に限定し、リポジトリや配布物に含めないでください。同じ Mac 上のコピーだけでは端末紛失への備えにはなりません。
- `SPARKLE_KEY_ACCOUNT` の既定は `jp.btajp.narumi` です。Keychain の鍵とコミット済み公開鍵の不一致は出荷エラーにします。鍵の変更・紛失時の移行は別途設計が必要で、`--allow-pubkey-rotation` による検査回避は認めません。

### 更新の E2E（ローカル）

旧開発用ハーネスは、更新後のデータルート隔離と候補版の Python wheel の版合わせが未対応のため、現在は副作用が起きる前に実行を停止します。詳細は [app/e2e-updater/README.md](app/e2e-updater/README.md) を参照してください。正式な GitHub 配布物の検証とは別の手順であり、現行版の旧版→新版の実更新は未検証です。

## 開発ルール

- 契約（`contracts/`）が正本です。変更は契約 → `uv run pytest pipeline/tests/contracts` → 実装の順で行います。
- テストは実エンジン / 実プロバイダ / 実録画に依存せず、`fake` 実装と ffmpeg の合成音声で通します。実エンジンの smoke テストは `-m real` で opt-in です。
- コミットは Conventional Commits（feat / fix / refactor / test / docs / chore）。

詳細は [AGENTS.md](AGENTS.md) と [docs/superpowers/specs/2026-08-27-narumi-foundation-design.md](docs/superpowers/specs/2026-08-27-narumi-foundation-design.md) を参照してください。
