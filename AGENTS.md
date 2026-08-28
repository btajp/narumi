# narumi

ローカル会議録画 → リッチ議事録生成システム。ワンボタン録画で、終了後に話者実名・スライド画像付きの議事録を自動生成する。相棒の gaia-library（記憶の索引 MCP）とは任意連携 — 無くても完結し、あれば精度が上がる。

設計の正本は Notion「議事録生成システム」ページ（2026-08-27 確定事項）。本ファイルはその要約と、リポジトリ内で守る開発ルール。基盤の具体設計（バンドル構成・manifest・契約形式・工程境界）は `docs/superpowers/specs/` を参照する。

## リポ構成
- `app/`: Swift ネイティブのメニューバー録画アプリ（ScreenCaptureKit）。マイクとシステム音声は必ず別トラックで録る（話者分離の第 1 層）。録画本体は CLI ヘルパー `narumi-recorder`（server がサブプロセス起動）、メニューバー UI は MCP クライアントとして server の公開ツールを呼ぶだけ
- `pipeline/`: Python（uv 管理）。パッケージ名 `narumi`。前処理・文字起こし・話者分離・突合・生成・エクスポート・バンドル・カタログ
- `server/`: MCP サーバー「narumi」（ローカル常駐 Streamable HTTP。dev は stdio）。パッケージ名 `narumi_server`。契約ファイルを起動時に読み込んでツール登録する
- `contracts/`: ツール契約（JSON Schema。1 ツール 1 ファイル＋共通 defs、`manifest.json` に contract_version=semver）。契約が正本。変更は契約 → 実装の順。破壊的変更は contract_version を上げる
- `scripts/`: dev スクリプト（server 起動、gaia-library のサブプロセス起動、型生成）
- 型生成: datamodel-code-generator → pydantic（`scripts/gen-types.sh`）。生成物（`pipeline/src/narumi/contracts/_generated/`）はコミットしない

## 絶対原則
1. ファイルが正本、DB は索引: 会議ごとに 1 セッションバンドル（`manifest.json` + `tracks/ preprocess/ transcripts/ diarization/ merged/ minutes/ context/`）。`narumi.db` は再構築可能なカタログ＋検索索引で、破損時はバンドルから全再構築できること
2. 決定的処理はスクリプト、LLM は読解・統合・生成のみ: 録画・ffmpeg・文字起こし・pHash・アライメントは固定手順。manifest に入力ハッシュ＋生成パラメータを記録し「同じ入力 → 同じ版」の冪等再生成を守る
3. UI = API パリティ: UI でできる操作はすべて MCP ツールとして公開する。UI は特権経路を持たない、ただの MCP クライアント。**アプリ（narumi.app）が最上位の操作面**で、ユーザーの操作は全部アプリだけで完結させる。製品 CLI `narumi` は契約から生成した MCP ツールの 1:1 写像（ライブラリ直叩きは `narumi-dev` のみ）。新機能は「アプリの操作 → 契約 → サーバー → CLI → アプリ」の順で足す（`docs/superpowers/specs/2026-08-27-narumi-surface-parity-design.md`）
4. 外部送信は会議プロファイルで明示制御: 既定はローカル完結（Whisper 系＋pyannote）。音声・テキストを外部に送るエンジン / LLM は能力プロファイル（送信先明記）＋明示オプトイン必須。`external_send_policy` に反するプロバイダ選択はエラーにする（黙ってフォールバックしない）
5. gaia-library への書き込みは propose_update 経由のみ（承認は人間ロールのみ）。内部専用経路は作らない

## パイプライン（会議 1 本の流れ）
録画（トラック分離）→ ffmpeg 分離 → 文字起こし（エンジン抽象化。既定ローカル Whisper）→ 話者分離 4 層（①トラック ②pyannote〔エンジン抽象化。安定後 sherpa-onnx への移行路あり〕③画面の発話中ハイライト判定 ④話者名付き外部トランスクリプト）→ 突合 2 段階（第 1 段: タイムスタンプ＋テキストアンカーの決定的アライメント／第 2 段: 区間ごと LLM 統合・語彙修正・話者実名化）→ 生成 → ローカル正本保存 → プラグイン型エクスポート（Notion / gaia-library / md / html）

## コンテキスト注入
- v1（全プロバイダ共通）: gaia-library に照会 → 返った参照を自分で巡回（Notion MCP / Box / ファイル）→「会議ブリーフ」（語彙・参加者・前回要点・背景）に整形し `context/` に保存 → 優先度順＋トークン予算で注入（予算は能力プロファイルからスケール）
- v2（tool_use 可のプロバイダ限定）: 統合中のエージェント型プル可。プル結果は必ずブリーフに追記保存（冪等性の回復）。不可環境は v1 に自動フォールバック

## LLM プロバイダ
Codex App Server / Claude Agent SDK / ローカル LLM / API 課金の 4 系統を抽象化。能力プロファイル（Vision・コンテキスト長・コスト・送信先）で工程ごとに選択。既定は追加コストゼロ構成

## 開発ルール
- dev スクリプトで gaia-library をサブプロセス起動する。ただし gaia-library 無しでも全テストが通ること（任意依存）
- 第 1 段（決定的）と第 2 段（LLM）は独立にテスト。LLM 工程は固定プロンプトのスナップショットテスト。テストは実エンジン / 実プロバイダに依存せず `fake` 実装で通す（実エンジンは opt-in の smoke テスト）
- アップグレード再生成 = 「対応表に 1 列追加 → 影響区間だけ第 2 段を再実行」。全体作り直しの実装は禁止
- 依存追加は `pipeline/pyproject.toml` / `server/pyproject.toml` の該当 extras に入れ、重い ML 依存は base に含めない
- 契約を変えるときは `contracts/` を先に更新し、`uv run pytest pipeline/tests/contracts` で整合を確認してから実装に着手する
- コミットは Conventional Commits（feat / fix / refactor / test / docs / chore）。`Co-Authored-By` は付けない
- 動作確認はインストール済みアプリの更新で行う。確認可能な機能単位でコミット・push・新バージョン公開まで進め、後続機能の完成を待って配布をまとめない
- 公開前にテスト・署名・公証・配布物照合を通す。アプリ画面や OS 許可の実機確認は公開後の Sparkle 更新で行い、開発ビルドの手動コピーを確認の前提にしない。未実装・未検証の範囲はリリースノートに明記する

## コマンド
```sh
uv sync                                                  # 初回セットアップ（.venv はリポ直下。dev グループに軽量 extras 込み）
uv run pytest                                            # Python 全テスト（pipeline + server）
uv run ruff check . && uv run ruff format --check .      # Lint / フォーマット
uv run narumi --help                                     # 製品 CLI（契約から自動生成。サーバー自動検出、無ければ in-process）
uv run narumi-dev --help                                 # dev CLI（ライブラリ直叩き。バンドル処理・doctor・カタログ再構築）
uv run narumi-server --stdio                             # MCP サーバー（stdio dev モード）
uv run narumi-server --http --port 8765                  # MCP サーバー（Streamable HTTP 常駐）
scripts/gen-types.sh                                     # 契約 → pydantic 型生成
cd app && swift build && swift test                      # 録画アプリ / recorder CLI（swift test は XCTest を含む Xcode が必要。CLT のみの環境では DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer を付ける）
```
