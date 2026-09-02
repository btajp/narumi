# narumi

narumi は、macOS 上でローカルに会議を録画し、終了後に議事録を自動生成するシステムです。アプリから画面・マイク・システム音声を **別トラック** で録画し、ffmpeg による分離 → 文字起こし → 話者分離 → 突合 → 生成 → エクスポートの工程を、会議ごとの「セッションバンドル」（ファイルが正本）に積み上げます。決定的な処理はスクリプトで固定手順として実行し、LLM は読解・統合・生成にだけ使います。既定はローカル完結（Whisper 系）で、音声やテキストを外部へ送るエンジン / LLM は会議ごとの `external_send_policy` で明示的にオプトインしない限り使えません。

相棒の **gaia-library**（記憶の索引 MCP）とは任意連携です。gaia-library が無くても narumi 単体で完結し、接続すると語彙・参加者・前回要点などを会議ブリーフに取り込みます。議事録の書き戻しは提案キューへの登録だけで、人間の承認は gaia-library 側で行います。設計の正本は Notion「議事録生成システム」ページで、リポジトリ内の要約と開発ルールは [AGENTS.md](AGENTS.md)、基盤の具体設計は [docs/superpowers/specs/](docs/superpowers/specs/) にあります。

**0.6.0** では、AI 接続を Codex App Server / Claude Agent SDK / OpenAI API / OpenAI互換API / Anthropic API / Ollama の順に統一し、全 6 種で接続・モデル選択・単一プロバイダによるテキスト議事録生成に対応しました。既定のローカル Whisper と OpenAI API 音声認識は工程別に選択できます。全工程のモデル選択、API の画像入力、複数プロバイダによる生成・統合は後続です。

ソース検証と配布の公開状況は別に扱います。利用できる公開版は [GitHub Releases](https://github.com/btajp/narumi/releases) で確認してください。

## インストールと録画

配布版は Apple Silicon（arm64）・macOS 15 以降に対応します。

1. 既存の narumi がある場合は、先に [停止・退避の手順](#初回導入と失敗時の復旧) を済ませます。既存版がなければこの準備は不要です。その後、[GitHub Releases](https://github.com/btajp/narumi/releases/latest) の `narumi-<version>.dmg` を開き、`narumi.app` を隣の「Applications」へドラッグします。
2. `narumi.app` を開きます。初回は同梱ランタイムの準備が終わるまで待ちます。Python や uv、ソースコードの別途導入は不要ですが、初回の依存・モデル取得にはネットワークが必要です。
3. 上部の「診断」を開き、「録画の権限」でマイク・画面収録の「許可を求める」を押します。拒否済みの場合は「macOS の設定を開く」から許可します。設定から戻ると再確認され、「状態を再確認」でも更新できます。許可設定だけでは録画は始まりません。
4. 両方が許可済みになったら「録画開始」を押し、会議名を入力します。プロファイルと scope は空欄なら既定設定を使います。macOS がアプリの再起動を求めた場合は、その案内に従ってから再試行します。
5. 録画中は同じ画面に経過時間と「録画停止」が表示されます。終了後は会議一覧から結果を開けます。

ffmpeg / ffprobe は別途必要です。アプリの「診断」で検出状態を確認できます。初回導入後の更新は GitHub Releases を自動確認し、利用者の操作で適用します。手動確認はメニューバーの「narumi」→「アップデートを確認…」です。毎回 DMG を入れ直す必要はありません。録画中やアプリが把握している処理の実行中は更新を延期します。別の CLI / MCP クライアントから処理している間は更新しないでください。会議データはアプリ本体とは別の Application Support 配下に保存するため、通常の更新で削除されません。

## 開発環境の要件

- macOS 15 以降（マイク取り込みに ScreenCaptureKit の `captureMicrophone` を使うため）
- [uv](https://docs.astral.sh/uv/)（Python 3.12 以降を管理。`.venv` はリポジトリ直下に作られます）
- ffmpeg / ffprobe（`brew install ffmpeg`）
- Xcode Command Line Tools（`xcode-select --install`。録画アプリのビルドに Swift 6.0 以降が必要。6.3.3 で検証）。`swift test` だけは XCTest を含む Xcode 本体が必要で、`xcode-select` が CLT を指す環境では `DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer swift test` で実行する（`swift build` は CLT だけで通る）
- 任意: gaia-library（無くてもテストは全て通ります）

## 開発環境のセットアップ

```sh
git clone <this repo> narumi && cd narumi
uv sync                      # Python 依存（pipeline + server。dev グループに軽量 extras 込み）
uv run narumi-dev doctor     # ffmpeg / recorder / エンジンの状態を確認
cd app && swift build -c release && cd ..   # 録画・Keychain ヘルパーも生成
```

`uv run narumi-dev doctor` は ffmpeg / ffprobe が見つからないと終了コード 1 で失敗します。録画ヘルパーは `NARUMI_RECORDER` 環境変数、無ければ `app/.build/{release,debug}/narumi-recorder` の順で探します。

## 開発用デスクトップビルド

```sh
scripts/build-app.sh     # app/ を release ビルドし、dist/narumi.app を組み立てて ad-hoc 署名
open dist/narumi.app     # メイン画面と、名前付きのメニューバー項目を表示
```

`narumi.app` は起動時に `narumi-server`（認証付き Streamable HTTP、既定 `https://127.0.0.1:8765/mcp`）を自分で起動し、終了時に停止します。アプリは所有者専用の起動情報から証明書とサーバー識別情報を確認し、Keychain のトークンで認証します。Terminal でサーバーを起動しておく必要はありません。起動方法は 2 モードあります（詳細は [app/README.md](app/README.md) の「ランタイムモード」）。

- **repo モード**（開発用。上の `scripts/build-app.sh` のとおりオプション無しでビルドした場合）: アプリがリポジトリの `uv run narumi-server` を起動します。動かすマシンには **uv と、このリポジトリのチェックアウト（`uv sync` 済み）** が必要です。
- **bundled モード**（配布用。`scripts/build-app.sh --runtime` でビルドした場合）: `.app` が同梱の uv・wheels・requirements から `NARUMI_HOME/runtime/` に venv を自分で作って起動します。固定版 Codex も `.app` に含みます。**リポジトリのチェックアウト、uv、Codex CLI の事前インストールは不要**です（Python 依存を初回取得するためネットワークは必要。後述「配布」参照）。

- `NARUMI_RUNTIME_MODE=repo|bundled` と明示的な `NARUMI_REPO` 指定を除き、同梱ランタイムがあれば bundled モードを優先します。以前保存したリポジトリ設定だけでは配布版を開発モードへ切り替えません。
- 開発用 repo モード、または `NARUMI_SERVER_URL` を明示した場合は、同じデータルートの起動情報を持つローカル常駐サーバーへ接続できます。URL は起動情報と一致する数値 loopback の HTTPS に限ります。通常の bundled 起動で既存サーバーがポートを使用している場合は、競合として表示します。
- ポートは `NARUMI_SERVER_PORT`、データルートは `NARUMI_HOME` で変えられます。ログは `~/Library/Logs/narumi/server.log`（メニューの「ログを開く」）。
- 詳細（起動コマンド、状態表示、終了時の録画確定）は [app/README.md](app/README.md) の「起動フロー」を参照してください。

## パイプライン概要

```
録画（screen.mp4 / mic.m4a / system.m4a を別トラック）
  → preprocess   ffmpeg で 16kHz mono wav に分離（決定的）
  → brief        会議ブリーフ context/brief.json（Gaia 接続設定がある場合だけ照会。語彙は transcribe / integrate、本文は minutes プロンプトへ）
  → transcribe   文字起こし（既定はローカル Whisper。明示選択した場合だけ OpenAI API を使用）
  → diarize      話者分離 第 1 層 = トラック（me / other）、第 2 層 pyannote（既定 none）、第 4 層 = 外部トランスクリプトの話者名
  → slides       キースライド抽出（画面トラックがあるときだけ。フレーム抽出 → pHash 重複除去 → preprocess/slides.json）
  → align        第 1 段: タイムスタンプ＋テキストアンカーの決定的アライメント（own / ext 全系統の区間対応表）
  → layer3       第 3 層 = 画面の話者ハイライト読解（vision 対応プロバイダが送信ポリシーで許可されたときだけ）
  → integrate    第 2 段: 区間ごとの LLM 統合（単一系統なら素通し。merged/integrate_cache.json で影響区間だけ再実行）
  → generate     議事録 minutes/vN/（版は増えるだけ。上書きしない。キースライド画像とブリーフを埋め込む）
  → export       プラグイン型エクスポート（markdown / html / notion / gaia-library）
```

各工程は `Bundle.run_stage(key, inputs, params, ...)` で実行され、`manifest.json` に入力ハッシュと生成パラメータを記録します。同じ入力＋同じパラメータなら再実行はスキップされ、「同じ入力 → 同じ版」が守られます。dev CLI の `regenerate` は align → integrate → generate だけを再実行します。MCP ツールの `regenerate` は、未処理・失敗した上流工程（前処理・ブリーフ・文字起こし・話者分離・スライド抽出）と、設定変更や `register_context` で inputs / params が変わった工程だけを冪等に実行してから align 以降を再実行します（`force` でも上流工程は強制しません）。

議事録用の接続・モデルを指定した場合は、6 系統とも `force=true` を使えません。モデル選択だけの変更は文字起こし・発話統合をやり直さず、同じ入力と選択では保存済み議事録を再利用します。新しい試行が必要な場合は、後述の [試行番号による再生成](#接続指定の設定保存と再試行) を使います。

### 実装状況

| 項目 | 状態 |
|---|---|
| セッションバンドル / manifest / 冪等ステージ実行 | 実装済み |
| 契約（`contracts/`）と MCP サーバー（契約 6.0.0・38 ツール） | 実装・ソース検証済み。6 プロバイダと明示的なモデル検証を公開。常駐は認証付き TLS、外部 MCP クライアント向け stdio bridge、開発用 stdio |
| 操作面パリティ拡張（録画状態・議事録取得・全文検索・既存録画の取り込み・プロファイル＋自動エクスポート・トラック破棄・会議削除・ジョブ取消・カタログ再構築） | 実装済み（MCP ツール＋CLI＋アプリ本体ウィンドウ） |
| 製品 CLI `narumi`（契約から自動生成の 1:1 写像） | 実装済み（常駐サーバーの証明書・認証を検証。条件を満たすローカル操作のみ in-process 可） |
| 録画アプリ / `narumi-recorder` | 実装済み（メイン画面とメニューバーから同じ MCP 録画操作を利用） |
| `narumi.app` によるサーバーの起動・停止（Terminal 不要） | 実装済み（repo モード＝uv とチェックアウトが必要 / bundled モード＝`--runtime` ビルドで自己完結） |
| ランタイム同梱 .app と Sparkle 自動更新 | 実装済み（Developer ID 署名・公証・公開物照合を行う配布手順。ffmpeg / ffprobe は別途必要） |
| ffmpeg 分離 → Whisper → プレーン議事録（`narumi.pipeline`） | 実装済み（`fake` エンジンで E2E テスト済み。実エンジンは `-m real` の opt-in smoke） |
| 外部トランスクリプト突合（Notion AI / Zoom / Meet / Teams） | 実装済み（WebVTT / SRT / Zoom txt / プレーンの決定的パーサ。`register_context` が即時パースして `transcripts/ext-*` として突合に参加、話者名は第 4 層で実名解決。URL は参照保存のみで取得は未実装） |
| キースライド抽出（pHash）/ Vision 読解 / 第 3 層話者判定 | 実装済み（pHash は pillow の自前実装、議事録へ画像埋め込みまで。第 3 層は vision 対応プロバイダが送信ポリシーで許可されたときだけ実行。実 vision プロバイダでの実機確認は未了） |
| gaia-library 照会によるコンテキスト注入（会議ブリーフ） | 実装済み（実契約の scope 付き照会と案件名から ID への解決。参照結果を `context/brief.json` に保存し、語彙・参加者・背景を注入。契約版とクライアントの識別情報も確認） |
| Gaia 接続設定 | 実装済み（アプリの「Gaia 接続」から URL・API キーの保存、無効化、接続テスト。MCP と CLI にも同じ操作を公開） |
| AI プロバイダ接続・認証・モデル候補 | Codex App Server / Claude Agent SDK / OpenAI API / OpenAI互換API / Anthropic API / Ollama の 6 系統を実装・ソース検証済み。API キーは Keychain、Codex のログイン情報は接続専用領域 |
| 接続とモデルを指定したテキスト議事録生成 | 全 6 系統の単一プロバイダ生成を実装・ソース検証済み。会議設定・プロファイルから接続・モデル・対応パラメータを指定し、送信許可を確認して保存・再生成 |
| OpenAI 音声認識・区間ごとの再開 | 実装・ソース検証済み（`whisper-1` / `gpt-4o-transcribe-diarize` のモデル選択、音声送信確認、成功区間の再利用・不明区間の明示再送） |
| Notion / gaia-library エクスポーター | 実装済み（Notion は REST でページ作成＋Markdown→ブロック変換。スライド画像のアップロードと実環境検証は未対応。gaia-library は `propose_update` の提案キューのみ＝絶対原則 5。実プロセスで認証・scope・提案の重複防止・agent の承認拒否を検証） |
| アップグレード再生成（影響区間だけ第 2 段を再実行） | 実装済み（`merged/integrate_cache.json` の区間フィンガープリントで、追加ソースが触れた区間だけ LLM を再実行） |

接続・モデル設定を適用するのは、**OpenAI 音声認識と、6 系統による単独のテキスト議事録生成**です。全工程のモデル選択、API の画像入力、複数プロバイダでの生成・統合は未対応です。Claude Agent SDK は固定版を隔離プロセスで実行し、API キー方式だけを扱います。接続テストやモデル候補の表示は、実認識・実生成の成功を意味しません。

**0.5.0 のソース検証結果**（2026-08-29）は次のとおりです。通常のテストでは fake とローカル TLS を使います。

| 検証対象 | 結果 |
|---|---|
| Python 全体 | 4,995 件成功、5 件 skip、2 件対象外（706.60 秒） |
| Swift 全体 | 691 件成功（27.43 秒） |
| ビルド・静的検査 | Python 2 パッケージの wheel、Swift build、Ruff check / format（328 ファイル）、compileall、製品 0.5.0・契約 5.0.0・37 ツールの整合が成功 |

fake HTTP と `MCPClient` / `NarumiClient` の実装を組み合わせ、旧契約 3 / 4 の互換性、ASR の自動再送を行わないこと、元の要求 ID・本文による手動復旧を確認しています。

以下は過去の **0.4.0 のソース検証結果**（2026-08-29）で、0.5.0 の検証結果ではありません。0.3.0 の検証結果も [実装計画の検証記録](docs/superpowers/plans/2026-08-28-provider-workflow.md) に保持しています。

| 検証対象 | 結果 |
|---|---|
| Python 全体 | 3,844 件成功、5 件 skip、2 件対象外（532.57 秒） |
| Swift 全体 | 565 件成功 |
| ビルド・静的検査 | Python 2 パッケージの wheel、Swift build、Ruff check / format（299 ファイル）、0.4.0 の版整合が成功 |

固定版の実 Codex CLI **0.150.1** でも、loopback 以外の通信を禁止した合成 fixture で、指定モデル・`tools=[]` を確認しました。成功応答とヘッダー段階・ストリーム途中の切断は、各ケースで生成 POST が 1 回でした。切断時は結果不明とし、自動再送しないことを確認しています。再現手順は `uv run python scripts/check_codex_protocol.py` です。この検証ケースの結果と、後述する固定 SDK の通信上の制約は分けて扱います。

上記は偽の API キーを使うソース・プロトコル検証です。実 ChatGPT ログイン・実生成、実 API・音声認識精度・区間境界の品質・実 Keychain、アプリ更新後の画面操作は未検証であり、署名・公証・配布物照合や公開成功を示すものではありません。

## コマンド

```sh
uv sync                                                  # 初回セットアップ
uv run pytest                                            # Python 全テスト（pipeline + server）
uv run ruff check . && uv run ruff format --check .      # Lint / フォーマット
uv run narumi --help                                     # 製品 CLI（契約から自動生成。サーバー自動検出）
uv run narumi-dev --help                                 # dev CLI（ライブラリ直叩き。デバッグ用）
uv run narumi-server --stdio                             # 独立した開発用サーバー（接続管理・秘密入力は不可）
uv run narumi-server --stdio-bridge                      # 起動済みの常駐サーバーへ接続（MCP クライアント用）
uv run narumi-server --http --port 8765                  # 認証付き TLS で常駐（narumi-keychain が必要）
scripts/dev.sh                                           # 同じ常駐サーバーを起動（GAIA_LIBRARY_CMD があれば gaia-library も）
scripts/build-app.sh && open dist/narumi.app             # メニューバーアプリ（サーバーはアプリが起動する）
scripts/gen-types.sh                                     # 契約 → pydantic 型生成（生成物はコミットしない）
cd app && swift build && swift test                      # 録画アプリ / recorder CLI
```

### AI プロバイダの接続

メインウィンドウ上部の「AI 接続」から接続を保存し、認証とモデル候補を確認します。議事録へ適用する接続・モデルは、会議の「設定」または「プロファイル」で別に保存します。

| プロバイダ | 認証・接続先 | この版の範囲 |
|---|---|---|
| Codex App Server | 専用の ChatGPT ログイン。API キー不要。OpenAI へ接続 | 接続・モデル選択・テキスト議事録生成 |
| Claude Agent SDK | API キー。`https://api.anthropic.com` 固定 | 固定版の隔離実行・明示的なモデル検証・テキスト議事録生成。サブスクリプションログインは未対応 |
| OpenAI API | API キー。`https://api.openai.com` 固定。ChatGPT の利用枠とは別の API 課金 | 接続・モデル選択・テキスト議事録生成・音声認識 |
| OpenAI互換API | API キー、または数値 loopback に限り認証なし。接続先と Responses / Chat Completions を固定 | 明示的なモデル検証・テキスト議事録生成 |
| Anthropic API | API キー。`https://api.anthropic.com` 固定 | 接続・モデル選択・テキスト議事録生成 |
| Ollama | 認証不要。既定 `http://127.0.0.1:11434` | 接続・ローカルモデル選択・テキスト議事録生成 |

接続名は用途を区別できる名前を入力します。同じプロバイダの接続を複数保存できます。API キーが未準備なら空欄で保存でき、既存接続では空欄で現在のキーを維持します。API キーは `narumi-keychain` ヘルパーを介して Keychain に保存し、再表示しません。入力欄は保存の成功・失敗・画面を閉じる際に消去します。

Ollama の接続先を変更する場合は `127.0.0.0/8` または `[::1]` の数値 loopback URL（HTTP / HTTPS）のみです。`localhost`・外部ホスト・URL 内の認証情報・クエリは使えず、クラウド実行モデルは対象外です。OpenAI互換APIの外部接続は HTTPS と API キーを必須とし、認証なしは数値 loopback だけを許可します。準備処理は固定したアダプタや SDK の版・配布メタデータを確認し、SDK・Ollama 本体・モデルを新規導入しません。

「この接続を有効にする」をオフにすると、設定と認証情報を残して無効化します。「保存時に API キーを削除」や「この接続からログアウト」は選択した接続の認証情報だけを削除し、ほかのアプリや接続には触れません。接続の保存・認証確認・候補更新だけでは会議データを送信せず、生成もしません。

保存の応答を失った場合は、一覧を再読込して保存済み接続の現在の内容を確認し、明示的に採用して再編集できます。接続を特定できない場合は、元の要求 ID で保存を再確認・再試行します。キーを送った要求では前回と同じキーの再入力が必要で、自動再送はしません。同じアプリ起動中は、画面を閉じ直しても非秘密の復旧情報を保持します。

### OpenAI API で音声を文字起こし

OpenAI 接続の準備 → 音声認識モデルと言語を選択 → 音声送信を許可して保存 → 再生成で内容を確認、の順に進めます。ローカル Whisper を使い続ける場合は変更不要です。

1. **接続を準備**: 「AI 接続」で OpenAI API のキーを保存し、「確認・準備」と「モデル一覧で接続を確認」を完了します。既存の OpenAI 接続を共用できます。Codex の ChatGPT ログイン情報は使わず、ChatGPT の利用枠とは別の API 課金です。
2. **音声認識モデルを選択**: 会議の「設定」または「プロファイル」で、「文字起こしの処理方法」を「OpenAI API を使う」にします。「音声認識用の OpenAI 接続」を選び、「音声認識の候補を取得・更新」から下表のモデルを選択します。議事録用の `llm` 候補と音声認識用の `transcription` 候補・キャッシュは別です。画面を開くだけでは保存済み候補を読み、音声は送りません。
3. **言語を設定**: 共通の「言語（auto / ja / en など）」に `ja`・`en` など小文字2文字の ISO 639-1 コード、または `auto` を指定します。空欄は現在の値を維持し、未設定なら `ja` です。`auto` は API への言語指定を省略します。追加パラメータの入力欄はなく、`parameters` は空の `{}` です。
4. **送信を許可して保存**: 「外部送信ポリシー」を `api_ok` にし、マイク・システム音声を別々に送ることと、両トラック分の API 利用料が発生し得ることを確認します。保存だけでは送信しません。ローカル文字起こしエンジン・話者分離・マイク本人名・議事録モデルは、別に変更しなければ維持します。語彙ヒントは空欄でもよく、API 音声認識へは送らず、ローカル文字起こし・発話統合で使います。
5. **確認して再生成**: 議事録タブの「再生成」で音声認識と後段の議事録生成の設定を確認し、開始します。プロファイル保存は既存会議を変更しませんが、新しい録画・取り込みで自動処理を有効にすると、その処理時に音声を送ります。

| 音声認識モデル | 利用する結果 | 話者の扱い |
|---|---|---|
| `whisper-1` | API が返す発話区間・単語の時刻 | この文字起こしだけでは話者の実名を確定しない |
| `gpt-4o-transcribe-diarize` | 時刻付きの発話区間・匿名話者ラベル | トラック・区間ごとのラベル。実名や別区間の同一人物とはみなさない |

既定のローカル処理へ戻すには「ローカルの設定を使う」を選んで保存します。API の選択だけを解除し、保存済みの `transcription_engine` を使います。設定保存は `expected_config` で変更前の全設定を照合し、競合時は上書きせず再読込を求めます。保存の応答が不明な場合も、再読込するまで成功・未適用を決めつけません。

音声は前処理済みの 16 kHz・mono・PCM16 WAV を最長10分の固定区間へ分け、マイク → システム音声の順に処理します。全トラック合計で最大144区間・24時間、送信ファイルはヘッダー込み24,000,000 bytes以下です。入力 WAV の音声 sample 以外のメタデータは合計1 MiB以下に制限し、送信用 WAV には含めません。これらはアプリ側の制限であり、料金上限ではありません。

送信先は `https://api.openai.com/v1/audio/transcriptions` 固定です。保存した接続のキーだけを使い、環境プロキシ・リダイレクト・自動再送を禁止します。会議名・元ファイル名や絶対パス・語彙ヒント・話者名指定・参照音声は追加送信しませんが、音声中の発言には名前などが含まれ得ます。議事録用の `store`・`reasoning`・出力トークン上限は音声 API に転用しません。利用量が欠ける場合は不明とし、ゼロや無料と推測しません。

時刻は API の native 値に区間開始時刻を加えて会議全体へ戻し、推測で補完しません。匿名話者ラベルは、既存の話者分離、マイクの本人判定、明示的な実名対応を上書きしません。成功区間は hash を確認して保存・再利用し、全区間が完了したトラックだけを文字起こしの正本へ反映します。別トラックの追加で計画が変わっても、共通の区間台帳から成功・結果不明を引き継ぎます。

### 音声認識の結果不明と再送

送信後の切断・タイムアウト・不正応答・保存失敗・取消は結果不明として保持します。アプリやサーバーの再起動、時間経過、DB 再構築、設定解除・復元、試行番号の増加だけでは自動再送しません。取消は通信を切る操作であり、サービス側の処理や課金の停止は保証しません。

1. **対象を確認**: 文字起こしタブの「不明区間を再送…」を開き、トラック・区間の時刻・完了済み数・重複課金の可能性を確認します。
2. **1区間の再送を許可**: 「API への再送を開始」を押します。アプリは音声認識の試行番号を変更前の設定と照合して保存し、保存後の全設定が確認内容と一致する場合だけ再送を要求します。
3. **結果を確認**: 成功区間は再利用し、許可した1区間と未処理分だけを続行します。別の結果不明区間や、同じ区間の再失敗には新しい確認が必要です。これは元の結果を回収する操作ではありません。

MCP / CLI の `transcription_retry` は、全体入力・対象区間の指紋と `blocked_epoch` を照合する1回限りの確認です。再送の pending 保存時に消費し、結果不明のままでも同じ確認を使い回せません。`cache_epoch` だけの変更、通常の再生成、自動処理からこの確認を迂回できません。

**再送要求自体の受付が不明な場合**は、ジョブ一覧の「文字起こしタブで元の要求を確認」から戻り、「同じ要求を再送して受付を確認…」→「同じ要求を1回再送」を使います。アプリが保持した元の要求 ID・本文だけを再送します。読み取り専用の照会ではなく、未受付だった場合は音声処理・課金が始まる可能性があります。成功応答とジョブ ID を得るまで元の受付不明を保持し、手動再送がエラーになっても未受付とは判断しません。自動再送は行いません。

受付不明の復旧情報はアプリのメモリに保持し、アプリ終了をまたぐ永続化はしません。サーバーの成功・結果不明の区間台帳は残り、通常の処理では結果不明の音声を自動再送しません。詳細は [音声認識の保存と再開](docs/superpowers/specs/2026-08-29-openai-transcription-design.md#保存と再開) を参照してください。

### 接続とモデルを指定してテキスト議事録を生成

接続の保存 → 準備・認証とモデル候補の取得 → 議事録の設定保存 → 内容を確認して再生成、の順に進めます。

1. **接続を保存**: 「AI 接続」でプロバイダと接続名を選び、「接続を追加して保存」を押します。Claude SDK / OpenAI / Anthropic / 外部のOpenAI互換APIは API キーを入力します。Codex と Ollama、数値 loopback で認証なしにした互換APIはキー不要です。
2. **準備・接続確認**: 「実行環境」の「確認・準備」を完了します。各接続の「モデル一覧で接続を確認」または「接続テスト」を使います。Codex は [専用ログインの手順](#codex-でテキスト議事録を生成) を先に行います。メタデータ取得の成功は残高・生成権限・実生成の成功を保証しません。
3. **モデル候補を取得**: 「接続先から候補を更新」を押します。画面を開くだけでは保存済み候補を読み、更新時だけ接続先へモデル情報を照会します。会議内容は送信しません。
4. **議事録の設定を保存**: 会議の「設定」または「プロファイル」で、「議事録の生成方法」を「接続とモデルを指定」にします。「議事録プロバイダ」→「保存済み接続」→「議事録モデル」の順に選び、下表のパラメータと外部送信ポリシーを確認して保存します。この画面の「モデル候補を取得・更新」からも候補を更新できます。保存だけでは送信・生成しません。
5. **内容を確認して再生成**: 議事録タブの「再生成」で、接続・モデル・パラメータ・送信先・利用料と送信内容を確認し、「再生成を開始」を押します。入力は文字起こし・話者名・会議名・議事録用コンテキストのテキストだけです。理由欄は任意で、空欄でも構いません。この議事録生成には音声・動画・画像を渡しません。

| 議事録プロバイダ | 設定するパラメータ | 外部送信ポリシー |
|---|---|---|
| Codex App Server | 対応モデルの「推論量」。未指定なら「モデルの既定値」。出力トークン上限は指定不可 | `subscription_ok` または `api_ok`。どちらも ChatGPT の利用枠を使い、API 認証へ切り替えない |
| Claude Agent SDK | 追加パラメータなし。出力トークン上限は指定不可 | `api_ok` を明示。Anthropic API の従量課金 |
| OpenAI API | 対応モデルの「推論量」と「出力上限（トークン）」 | `api_ok` を明示。ChatGPT の利用枠とは別に API 課金 |
| OpenAI互換API | 「出力上限（トークン）」。接続時に固定した API surface と token field を使用 | `api_ok` を明示。loopback でも外部中継の可能性を含む |
| Anthropic API | 「出力上限（トークン）」。推論量は今回は指定しない | `api_ok` を明示 |
| Ollama | 「出力上限（トークン）」。推論量は今回は指定しない | `local_only` のままで利用可。ローカル実行モデルの確認が必要 |

出力上限には 1〜32,768 の整数を入力します。空欄なら 4,096 と確認済みのモデル上限の小さい方を使います。モデル上限が不明なら 4,096 をアプリの既定値として使い、モデルの能力は未確認（`null`）のままです。既知のモデル上限を超える値は拒否します。この上限は 1 回の要求に適用し、分割処理を含む総利用量・金額の上限ではありません。OpenAI の推論モデルでは推論トークンも出力上限に含まれます。

`local_only` は自動変更しません。接続・認証・モデル能力が未確認なら選択を拒否し、別モデルや別の認証情報へ切り替えません。文字起こし・話者分離・「従来の LLM プロバイダ」は、この議事録設定では変更不要です。発話統合・画像読解には従来設定を使います。プロファイルの保存は既存会議を変更しませんが、そのプロファイルで新しい録画・取り込みを行い、自動処理を有効にすると処理時に選択先へテキストを渡します。

OpenAI はモデル一覧と固定能力表の両方で確認できた ID だけを選択可能にします。初期対象は `gpt-5.6-sol` / `gpt-5.6-terra` / `gpt-5.6-luna` / `gpt-5.4` / `gpt-4.1` / `gpt-4.1-mini` と、公式に確認した 3 つの日付付き ID です。未知のモデル・未確認の snapshot・fine-tune は名前から推測せず、能力未確認として扱います。推論量の候補もモデル別です。対象 ID と既定値は [モデルとパラメータ](docs/superpowers/specs/2026-08-29-openai-minutes-design.md#3-モデルとパラメータ) を参照してください。

OpenAI が返す `shutdown_date` は `availability_expires_on`（`YYYY-MM-DD`）として保持し、画面では「提供終了予定日」を表示します。narumi は UTC 日付が当日以降になったモデルを拒否します。これはアプリ側の保守的な判定であり、公式の正確な終了時刻・タイムゾーンを保証するものではありません。

### Codex でテキスト議事録を生成

接続追加 → 実行環境の準備 → 確認コードで専用ログイン → モデル選択と送信許可の保存 → 再生成、の順に進めます。

1. **接続を追加**: 「AI 接続」で「Codex App Server」と接続名（例: `議事録用 Codex`）を選び、「接続を追加して保存」を押します。API キーは不要で、既存 Codex の認証情報を流用しません。
2. **実行環境を準備**: 「実行環境」の「確認・準備」を使います。配布版は `.app` に同梱した公式 Codex CLI **0.150.1** を専用領域へコピーし、固定した版・サイズ・SHA256 を確認します。この準備と版確認は外部通信せず、グローバルツールを変更しません。同梱物を検証できない場合は停止し、外部の Codex へ切り替えず、公式配布版の更新または再インストールを案内します。repo モードだけは、従来どおり対応版の既存 npm インストールを検査できます。
3. **専用ログイン**: 準備後に「ChatGPT でログイン」を押すと「確認コード」が表示されます。「ブラウザで続ける」で [OpenAI のデバイスログイン画面](https://auth.openai.com/codex/device) を開き、利用者がそのコードを入力して承認します。「確認コードをコピー」は必要な場合だけ使います。API キーの入力は不要です。
4. **モデル候補を取得**: 「接続先から候補を更新」を押します。画面を開くだけでは保存済み候補を読み、更新時にモデル情報を照会します。会議内容は送信しません。候補取得の成功と、実際の生成成功は別です。
5. **会議設定・プロファイルへ保存**: 上記の共通手順で「議事録の生成方法」を「接続とモデルを指定」、「議事録プロバイダ」を「Codex App Server」にします。「保存済み接続」「議事録モデル」「推論量」を選び、`subscription_ok` または `api_ok` を明示して保存します。`api_ok` でも Codex が API キー方式へ切り替わることはなく、ChatGPT の利用枠を使います。保存だけでは送信・生成しません。
6. **送信内容を確認して再生成**: 議事録タブの「再生成」で接続・モデル・推論量・送信先と内容を確認し、「再生成を開始」を押します。送るのは文字起こし・話者名・会議名・議事録に使うコンテキストのテキストです。理由欄は任意で、空欄でも構いません。Codex の議事録生成には音声・動画・画像を送信しません。

ログイン用 URL と確認コードは一時表示で、完了・取消・失敗・画面を閉じる・結果不明の際にアプリ内の表示と保持を消去します。応答が途切れた場合は「認証操作の状態を確認」を使い、新しいログインを自動で開始しません。アカウントや組織でデバイスログインが無効な場合はエラーで停止し、ブラウザのコールバックを使う別のログイン方式や API キー方式へ自動変更しません。

認証情報は narumi の接続専用領域に保存します。普段の Codex アプリ・CLI の認証・設定を共有せず、他アプリのログイン用コールバックポートも使いません。

Codex の生成ではモデルに渡す tools を空にし、会話履歴を保存しない構成を使用します。ログイン・モデル照会・生成に必要な通信は行います。管理設定を検出した場合や固定した実効設定を確認できない場合は利用を拒否し、任意の管理環境で安全性を保証するものではありません。

通常の要求・ストリームの再試行は無効にしていますが、固定版 SDK 内の HTTP リダイレクト、401 応答後の認証回復、未処理が確定した HTTP/2 NACK の再送は残ります。すべての通信が同じ URL に一度だけ送られることを保証するものではありません。narumi が結果不明の生成を自動で開始し直さないこととは、分けて扱います。

### API・ローカル生成の通信

OpenAI API は `https://api.openai.com/v1/responses`、Anthropic API は公式の Messages API、Ollama は確認済みの数値 loopback 接続先へ、内蔵 HTTP アダプタで送ります。この HTTP 経路では環境プロキシ・リダイレクト・自動再送を使わず、期限・応答サイズ・取消を制御します。OpenAI は `tools=[]`、`tool_choice=none`、`store=false` を指定し、会話 ID や過去の応答 ID は送りません。`store=false` は、不正利用監視などサービス側の保持が一切なくなることを意味しません。

取消は接続を切る操作であり、サービス側の処理や課金の停止を保証しません。送信後の切断・不正な応答・成果保存の失敗は結果不明として扱います。API 応答に利用量がない場合は不明とし、ゼロや無料とは推測しません。

Ollama は送信前にローカルモデルの digest を再確認し、変更時は選び直しを求めます。ただし、モデルの Modelfile に組み込まれた system / messages が継承される場合があります。それらの隔離や、narumi のプロンプトだけで生成されることは保証しません。

### 接続指定の設定保存と再試行

`minutes_model` は議事録生成だけを変更する非秘密の設定です。従来の `llm_provider` は変更しません。

| 項目 | 保存する内容 |
|---|---|
| `provider` | `codex-app-server` / `claude-agent-sdk` / `openai-api` / `openai-compatible-api` / `anthropic-api` / `ollama` |
| `connection_id` / `connection_revision` | 選んだ接続と、その時点の版 |
| `model_id` | その接続から取得した候補のモデル ID |
| `parameters.reasoning_effort` | 選択モデルが対応する推論量。省略は候補の既定値 |
| `parameters.max_tokens` | OpenAI / OpenAI互換 / Anthropic / Ollama の 1 回あたりの出力上限。省略時の扱いは上記の共通手順を参照 |
| `cache_epoch` | 生成の試行番号。通常は `0` |

設定更新で `minutes_model` を省略すると保持、`null` なら解除、オブジェクトを渡すと選択全体を置き換えます。アプリでは「従来設定」を選んで保存すると解除します。接続版が変わった場合は「変更後の接続を選び直す」からモデルを再選択し、保存します。利用できない接続・モデル・推論量は拒否し、別モデルへ自動変更しません。

議事録用の接続・モデルを指定した会議の `regenerate` と、`auto_regenerate=true` の `register_context` には、確認した設定全体を `expected_config` として渡します。アプリは確認画面に表示した設定を送ります。CLI / MCP では `get_meeting` の `config` を使い、実行時に保存済み設定と一致しなければ、変更・ジョブ作成・外部送信の前に拒否します。

同じ入力・選択・実行環境では保存済みの結果を再利用し、6 プロバイダすべてで `force=true` は拒否します。呼出ごとに成功結果を checkpoint へ保存し、送信後に結果が不明になった場合は生成を自動で開始し直しません。新しく試す場合は会議設定の「前回の結果が不明・新しい試行が必要なとき」→「新しく生成を試す…」で、API 利用料や ChatGPT の利用枠を重複消費する可能性を確認し、「試行番号を増やす（保存後に再生成）」を押します。Ollama ではローカル処理を再実行します。この操作だけでは保存・送信せず、フォームを保存してから再生成します。過去の議事録は保持します。CLI / MCP では保存後の設定を `expected_config` に渡し、新しい `request_id` と `force=false` で再生成します。

### gaia-library との接続（任意）

gaia-library の HTTP サーバーを起動し、narumi 用の agent クライアントと API キーを用意しておきます。narumi のメインウィンドウで「Gaia 接続」を開きます。

1. **接続先 URL**: 同じ Mac 上の HTTP エンドポイントを入力します（例 `http://127.0.0.1:4111/mcp`）。外部への誤送信を防ぐため loopback 以外、URL 内の認証情報、クエリ、リダイレクトは拒否します。
2. **API キー**: gaia-library が発行したキーを入力して保存します。キー欄は再表示しません。同じ URL では空欄で既存キーを維持し、URL を変えた場合は旧キーを引き継ぎません。削除・連携無効化は専用の操作を使います。
3. **接続テスト**: 保存後に実行します。サーバーの契約版・クライアント名・既定 scope を確認するだけで、提案や承認は行いません。会議の scope / engagement は既存のプロファイル・会議設定で指定します。

保存先は `<NARUMI_HOME>/gaia.json`（所有者だけが読み書きできる `0600`）です。キーは暗号化せず保存するため、同じ OS アカウントからは読めます。プロファイル、会議バンドル、ツール応答にはキーを保存しません。保存済み設定を優先し、未保存の場合だけ `NARUMI_GAIA_URL` / `NARUMI_GAIA_API_KEY` を参照します。アプリで無効化した後は環境変数から再有効化されません。

未設定なら従来どおり単体で動作します。設定済みの接続が失敗した場合はエラーを表示し、別サーバーや未認証接続へ黙って切り替えません。既存ブリーフを再利用する場合も接続先の識別情報を確認するため、設定した gaia-library サーバーが必要です。

実 gaia-library との結合テストは `NARUMI_GAIA_BIN=/path/to/gaia uv run pytest pipeline/tests/test_gaia_live.py` で任意実行できます。一時設定・DB・キーを作成して検証し、ユーザーの既存データは使いません。環境変数を指定しなければスキップされます。

### 製品 CLI `narumi`

`narumi` は MCP ツールの **1:1 写像** です（`docs/superpowers/specs/2026-08-27-narumi-surface-parity-design.md`）。サブコマンドは `contracts/` から自動生成され（ツール名の `_` を `-` に置換）、オプションは `inputSchema` から型付きで生成されます（string / integer / number / boolean はそのまま、array / object は JSON 文字列、`scope` セレクタは名前 1 つか JSON 配列）。秘密入力は後述の非表示プロンプトか stdin を使います。`request_id` は省略すると UUID4 を自動発番します。

nullable な項目は `--clear-<項目>` で JSON `null` を明示できます。例えば `narumi set-gaia-connection --clear-url` は連携を無効化し、`--clear-api-key` はキーだけを削除します。省略は既存値を保持し、文字列 `null` を値として渡しても削除にはなりません。値と clear の同時指定は拒否します。既存オプション名と衝突する場合は clear 側に接尾辞を付けるため、正確な名前は `--help` で確認してください。

```sh
uv run narumi list-meetings --scope cloudnative --limit 10
uv run narumi configure-recording-permission --permission microphone --action request
uv run narumi get-server-info --refresh-permissions
uv run narumi list-providers
uv run narumi list-provider-connections
uv run narumi search-transcripts --query "オンボーディング"
uv run narumi get-minutes --meeting-id <meeting_id> [--version N]
uv run narumi import-recording --meeting-name "定例" --mic-path /abs/mic.m4a --system-path /abs/system.m4a
uv run narumi delete-meeting --meeting-id <meeting_id> --confirm
uv run narumi tool list_meetings --json '{"limit": 5}'    # 秘密情報を含まない JSON 引数
```

接続先はデータルート内の `runtime/server/bootstrap.json` から取得します。`--server-url` または `NARUMI_SERVER_URL` を指定する場合も、起動情報と一致する数値 loopback の HTTPS URL が必要です。Keychain のトークンで認証する TLS 接続を使い、証明書・サーバー識別情報・契約メジャー版を確認してから操作を送ります。常駐サーバーとの全通信で環境プロキシとリダイレクトを使いません。

自動で in-process 実行へ切り替えるのは、起動情報がなく、接続先も明示していない場合の一部のローカル操作だけです。録画・権限設定・プロバイダ関連の 10 ツール・秘密入力を持つツールは常駐サーバー必須です。`--require-server` は常駐接続を必須にし、`--in-process` でもこれらの制約は解除されません。起動情報の不正、証明書・認証エラー、通信断では in-process に切り替えず、実行要求も自動再送しません。

出力は結果 JSON（`--pretty` 既定、`--raw` で 1 行）で、エラーは契約の `error_envelope` を stderr に出し終了コード 2 で終わります。`--data-root PATH`（または `NARUMI_HOME`）は、常駐接続の起動情報と in-process 実行のデータルートを指定します。アプリと同じデータルートを使ってください。

API キーを保存する場合、`--api-key` は値を引数に取らず、非表示の入力プロンプトを開きます。自動処理では `--api-key-stdin`、ツール引数全体の JSON は `tool <tool_name> --json-stdin` を使います。キーをコマンド行、環境変数、`--json` の文字列に書かないでください。`--json` に秘密入力を含めると拒否します。キーの削除は `--clear-api-key`、省略は既存値の保持です。

```sh
uv run narumi set-provider-connection --provider-id anthropic-api \
    --display-name "議事録用 Anthropic" --auth-method api_key --api-key
```

権限設定はサーバーが動く Mac 上の操作です。`--action open_settings` は対象のプライバシー設定を開くだけで、許可を自動付与しません。応答が不明になった場合は要求を自動再送せず、`get-server-info --refresh-permissions` で、操作前と同じ `server_instance_id` の `permission_setup_in_progress` と権限の状態を確認します。別サーバーの未処理表示は、元の操作の終了証明にはなりません。この機能は契約版 1.1.0 以降が必要です。

6プロバイダの接続・モデル検証・生成には契約 6.x が必要です。配布アプリは同梱サーバーの契約 6.0.0 と厳密に照合し、製品 CLI は契約メジャー版 6 を要求します。開発用に外部サーバーへ接続する Swift アプリだけは、旧契約 2 / 3 / 4 / 5 も各版の対応範囲で許可し、新機能への対応を推測しません。通常はアプリ・サーバー・CLI を同じ対応版に揃え、未認証 HTTP へダウングレードしません。

音声認識の選択解除は `set-meeting-config --clear-transcription-model` です。`regenerate` と再生成付き `register_context` は、API 音声認識を選択した場合も確認済みの全設定を `--expected-config` に渡します。不明区間の再送確認は `regenerate` の `--transcription-retry` だけで受け付け、自動処理やコンテキスト登録では再送を許可しません。

Codex 接続の `--auth-method` は `chatgpt` で、API キー引数は使いません。議事録用選択の解除は `set-meeting-config --clear-minutes-model`、生成時の確認済み設定は `--expected-config` で指定できます。Gaia のキー保存形式は引き続き上記の `gaia.json` で、Keychain へは移行していません。

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
├── providers/registry.json  # プロバイダ接続・認証状態・モデル情報（キー本体は含まない）
├── providers/runtime/      # 既存依存の検査記録・準備済み Codex 0.150.1
├── providers/codex-connections/  # 接続ごとの Codex 専用認証・実行領域
├── runtime/server/         # 所有者専用の TLS 起動情報・証明書・秘密鍵
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

API 方式のプロバイダのキーと常駐接続トークンは Keychain に保存します。Codex の ChatGPT 認証情報は、接続専用の `providers/codex-connections/<connection_id>/state/` に所有者専用の権限で保存します。既存 Codex の認証・設定・会話はコピーしません。`bootstrap.json` は証明書・サーバー識別情報・不透明な Keychain account を持ちますが、トークン本体は含みません。これらの認証情報・起動情報・秘密鍵を MCP クライアント設定へコピーする必要はありません。

## MCP クライアント設定例

一般の MCP クライアントには **stdio bridge** を使います。先に `narumi.app` を起動し、同じデータルートの常駐サーバーへ `narumi-server --stdio-bridge` で接続します。bridge が起動情報・証明書・Keychain 認証を扱うため、設定にトークンや証明書を書きません。サーバーが使えない場合はエラーで止まり、独立したサーバーを自動起動しません。

以下はソース環境の例です。`/path/to/narumi` は `uv sync` と Swift ビルド済みのリポジトリへ置き換えます。`NARUMI_HOME` を変更している場合は、bridge にも同じ値を渡します。開発用の `--stdio` は別プロセスで実行するモードで、プロバイダ接続管理と秘密入力には使えません。

### Claude Code

```sh
claude mcp add --transport stdio narumi -- uv --directory /path/to/narumi run narumi-server --stdio-bridge
```

`.mcp.json` に書く場合:

```json
{
  "mcpServers": {
    "narumi": {
      "command": "uv",
      "args": ["--directory", "/path/to/narumi", "run", "narumi-server", "--stdio-bridge"]
    }
  }
}
```

### Codex

`~/.codex/config.toml`:

```toml
[mcp_servers.narumi]
command = "uv"
args = ["--directory", "/path/to/narumi", "run", "narumi-server", "--stdio-bridge"]
```

## 配布（ランタイム同梱 .app と自動更新）

設計は [配布・自動更新](docs/superpowers/specs/2026-08-27-narumi-app-distribution-design.md) と [初回 DMG・公開後確認](docs/superpowers/specs/2026-08-28-dmg-installer-release-design.md)。対象は Apple Silicon（arm64）・macOS 15 以降、配布経路は GitHub Releases のみ。

### ランタイム同梱 .app のビルド

```sh
scripts/build-app.sh --runtime    # dist/narumi.app（ランタイム同梱、ad-hoc 署名）
```

`--runtime` は `Contents/Resources/runtime/` に uv、narumi / narumi-server の wheel、ハッシュ付き `requirements.txt`、契約ファイル、Codex CLI 0.150.1、Apache-2.0 の LICENSE / NOTICE、runtime manifest を同梱します。Codex は OpenAI 公式 `rust-v0.150.1` の Apple Silicon 用 artifact を使用し、URL・tag commit・archive SHA256・展開後 SHA256・サイズ・arm64・OpenAI の Developer ID Team を `scripts/runtime.lock.json` で固定し、実行時 trust anchor との完全一致もビルド前に確認します。展開後 binary は 228,986,048 bytes（約 218.4 MiB）で、上流署名を保持します。署名前後の inventory と署名を検査し、未列挙ファイルや変更を拒否します。

初回起動と更新時に、必要に応じて `NARUMI_HOME/runtime/` の Python 3.13 と venv を準備します。**リポジトリのチェックアウト、uv、Codex CLI の事前インストールは不要**ですが、Python 依存の初回取得にはネットワークが必要で、ffmpeg / ffprobe は別途必要です。Codex の準備と版確認はオフラインで完了しますが、ChatGPT ログイン・モデル照会・議事録生成には通信が必要です。進捗はアプリに表示し、ログは `~/Library/Logs/narumi/runtime.log` に保存します。

版は `VERSION` ファイルが正本（`CFBundleShortVersionString`）、`CFBundleVersion` は `git rev-list --count HEAD` です。Python パッケージ、サーバー、録画ヘルパー、`CHANGELOG.md` の版との一致は `scripts/check-version.sh` で検査します。

### リリース（`scripts/release-app.sh <version>`）

Developer ID 署名 → Apple 公証 → DMG・Sparkle フィード生成 → GitHub Release（draft）までを行います。
署名・公証・配布物照合を通した draft を公開し、その後にインストール・更新・実機確認を行います。
確認可能な機能単位でコミット・push・新バージョン公開まで進めます。後続機能の完成や、配布前のアプリ画面確認を待ってリリースをまとめません。

1. clean な `main` と `origin/main` の一致、版、lock、既存リリース、署名鍵を検査します。Sparkle ツールは SwiftPM が取得したものを自動検出します。別の既存配置を使う場合だけ `SPARKLE_BIN` を指定します。
2. 署名には `APPLE_SIGNING_IDENTITY` / `APPLE_API_KEY` / `APPLE_API_ISSUER` / `APPLE_API_KEY_PATH` を使います。ローカル設定 `~/.config/narumi/release.env` があれば読み込みます。設定ファイルの場所を変える場合は `NARUMI_RELEASE_ENV` を指定します。秘密情報をリポジトリやログに記録しないでください。
3. 専用の `dist/release/v<version>/build/narumi.app` へビルドし、同梱 uv を含めて署名します。使用中の `dist/narumi.app` は置き換えません。wheel とアプリの収録ファイルも検査します。
4. 公証・staple 後に最終 ZIP を作り、再展開して署名と公証チケットを検証します。その ZIP の app から初回導入用 DMG を作り、DMG 自体も署名・公証・staple します。read-only でマウントし、内容とファイル権限が ZIP と一致することを確認します。
5. ZIP だけから EdDSA 署名と appcast を作り、版・build・公開 URL・長さ・SHA256 を照合します。`feed/` は ZIP と appcast の 2 件、DMG は `installer/` に分けて封印します。
6. 出荷元に変更がないことを再検査し、固定したコミット SHA を指定して draft を作成します。0.1.4 以降は ZIP・appcast・DMG の 3 件を添付し、実 assets を再取得して照合します。0.1.3 以前の検証は 2 件のままです。
7. `scripts/release-app.sh <version> --verify-draft` が成功したら、同じ release ID・`tag_name`・`target_commitish` を保持して公開します。この段階では UI や OS 許可の確認を待ちません。
8. `scripts/release-app.sh <version> --verify-published` で公開タグ・latest・匿名取得した feed / ZIP / DMG を照合します。初回は公開 DMG からインストールし、以後はアプリの Sparkle 更新後に起動・同梱サーバー・診断を確認します。配布物が不一致なら公開を取り下げて調査し、公開済み asset を上書きせず次の版で修正します。

別の出力先を使う場合は、最初から最後まで同じ `RELEASE_DIR` を指定します。初回出荷での「最新です」という表示は、旧版から新版への更新適用を検証したことにはなりません。

### 初回導入と失敗時の復旧

初回の置換前に録画・ジョブ・許可操作がないことを確認し、既存 app と所有 server を正常終了します。対象プロセス・process group・待受 port の停止を確認できない場合は置換しません。既存 app は `~/Library/Application Support/narumi/app-backups/<版>-<日時>/` に退避し、app の版・build・同梱 server の版・署名主体と退避先を記録します。復元候補は Developer ID 署名・公証を検証できた正式版に限ります。会議データや設定は初期化しません。

更新前後の app の版・build、server の版、起動結果を記録します。失敗時は「ログを開く」の `~/Library/Logs/narumi/server.log` と、環境準備の `runtime.log` を確認します。旧版が正常なら継続利用し、Sparkle から再試行します。server が起動せず更新も操作できない場合は、利用者の承認と対象プロセスの停止確認後に、検証済みの退避旧版へ復元します。安全な復元元がなければ停止して状況を報告します。手動復元を自動更新の成功とは記録しません。

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
