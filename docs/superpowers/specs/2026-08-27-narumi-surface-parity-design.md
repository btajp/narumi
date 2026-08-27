# 操作面のパリティ設計: アプリ ＝ MCP ＝ CLI（2026-08-27）

## 原則（2026-08-27 確定）
1. **アプリ（narumi.app）が最上位の操作面**。ユーザーがやりたいことは全部アプリだけでできる。UX 上の便利機能（プレビュー、ドラッグ＆ドロップ、進捗表示、Finder で開く）はアプリ固有でよい
2. **MCP はアプリの全操作を公開する**。アプリは MCP クライアントで、データに触れる経路は MCP ツールのみ（AGENTS.md 絶対原則 3）。したがって「アプリでできる ⊆ MCP でできる」は構造的に成立し、Swift テストで「アプリが呼ぶツール名 ⊆ 契約」を検査する
3. **CLI は MCP ツールの 1:1 写像**。契約から自動生成し、常駐サーバーがあればそこへ接続、無ければ in-process でハンドラを呼ぶ。ライブラリ直叩きのデバッグ用 CLI は `narumi-dev` として分離し、製品の操作面には数えない
4. 契約に無い操作をアプリ・CLI に足すときは**契約 → サーバー → CLI（自動）→ アプリ**の順

## アプリの画面・操作一覧（app-first）と必要ツール

| 画面 / 操作 | 使うツール | 状態 |
|---|---|---|
| メニューバー: 録画開始（会議名・プロファイル・scope）/ 停止 / 録画中の経過 | `start_recording` / `stop_recording` / `get_recording_status`（新規） | 実装済み（メニューバー: 会議名・プロファイル・scope 入力と経過表示。ウィンドウ: 録画中バナー + 停止） |
| 会議一覧（状態・進行中ジョブ・検索） | `list_meetings`（`active_job` を追加）/ `search_transcripts`（新規）/ `get_job_status` | 実装済み |
| 会議詳細: 議事録プレビュー（版切替）/ 文字起こし / コンテキスト / エクスポート履歴 / バンドルを Finder で開く | `get_meeting` / `get_minutes`（新規）/ `get_transcript` | 実装済み |
| コンテキスト登録（貼り付け・URL・ファイルドロップ）→ 必要なら再生成 | `register_context` / `regenerate` | 実装済み |
| 会議設定（エンジン / LLM / 送信ポリシー / 自分の名前 / 語彙 / scope） | `set_meeting_config` | 実装済み |
| 既定プロファイル（新規会議の既定設定・既定エクスポート先） | `list_profiles` / `get_profile` / `set_profile` / `delete_profile`（新規） | 実装済み |
| エクスポート（md / html、保存先選択、後から再エクスポート） | `export_minutes` / `list_export_destinations` | 実装済み |
| 既存録画ファイルの取り込み（Zoom ローカル録画等） | `import_recording`（新規） | 実装済み |
| 録画トラック破棄（文字起こし後に動画・音声を消す）/ 会議削除 | `discard_tracks` / `delete_meeting`（新規） | 実装済み |
| ジョブ取消 | `cancel_job`（新規） | 実装済み |
| 診断（ffmpeg / 権限 / エンジン / データルート）/ カタログ再構築 | `get_server_info`（`diagnostics` 追加）/ `rebuild_catalog`（新規） | 実装済み（診断シート） |
| サーバー起動・停止・再起動 / ログ / アップデート確認 | アプリ固有（プロセス管理・Sparkle。データ操作ではない） | 実装済み（メニューバー + 診断シート） |

## 契約 v1 への追加（初回リリース前なので contract_version は 1.0.0 のまま）

共通: 既存規約どおり（書き込みは `request_id`、読み取りは `readOnlyHint`、scope セレクタ、error_envelope）。

- `get_recording_status` {} → `{active, meeting_id?, meeting_name?, started_at?, elapsed_sec?, tracks?: {name: relpath}}`。readOnly
- `get_minutes` {meeting_id, version?（既定 latest）, scope?} → `{meeting_id, version, markdown, generated_at, provider, unresolved_speakers: [str], available_versions: [int]}`。readOnly
- `search_transcripts` {query（1 文字以上）, scope?, limit? 1..200 既定 20} → `{hits: [{meeting_id, meeting_name, source_id, segment_id, start, end, speaker?, text}]}`。readOnly。カタログの FTS（trigram）を使う
- `import_recording` {meeting_name, mic_path?, system_path?, screen_path?（絶対パス・通常ファイル。mic か system のどちらかは必須）, started_at?, engagement?, scope?, profile?, config?, copy?（既定 true。false はハードリンク）, auto_process?（既定 true）, request_id} → `{meeting_id, bundle_path, tracks: {name: track_status}, job_id?}`
- `discard_tracks` {meeting_id, tracks: [screen|mic|system]（1 つ以上）, scope?, request_id} → `{meeting_id, tracks: {name: track_status}}`。destructiveHint。録画中・ジョブ実行中は `busy`。mic / system は対応する `transcripts/own-<track>` が存在する場合のみ破棄可（`invalid_argument`）。screen はいつでも可（キースライド抽出前の破棄は将来の Step 4 で警告対象）。破棄は manifest に `discarded: true` として記録し、sha256 は残す
- `delete_meeting` {meeting_id, scope?, confirm: true（定数）, request_id} → `{meeting_id, deleted: true, moved_to}`。destructiveHint。バンドルを `<NARUMI_HOME>/trash/<meeting_id>-<timestamp>/` へ移動（即時完全削除はしない）し、カタログから除く。録画中・ジョブ実行中は `busy`
- `cancel_job` {job_id, request_id} → `{job}`。queued は即 `cancelled`、running は協調キャンセル（JobManager のフラグを pipeline の progress フックで検査し、`cancelled` エラーで中断。manifest.status は `failed` ではなく直前の状態に戻す）。エラーコードに `cancelled` を追加
- プロファイル: `list_profiles` {} → `{profiles: [profile], default: name}`、`get_profile` {name}、`set_profile` {name, config?: meeting_config, scope?, engagement?, export_destinations?: [str], make_default?, request_id} → `{profile}`、`delete_profile` {name, request_id}（default は削除不可）。正本は `<NARUMI_HOME>/profiles.json`。`start_recording` / `import_recording` の `profile` 省略時は default を適用し、明示引数が優先。process ジョブ成功時にプロファイルの `export_destinations` へ自動エクスポート（manifest.exports に記録）
- `rebuild_catalog` {request_id} → `{meetings, segments, errors: [str]}`。idempotent
- 変更: `meeting_summary` に `active_job?: {job_id, kind, status, progress?}`、`get_server_info` に `diagnostics: {ffmpeg: {path, version}|null, ffprobe: {...}|null, data_root, meetings_root, catalog_path, recorder_path|null, contracts_dir}`

## CLI（製品用 `narumi`）— 実装済み（2026-08-27）

- 実装は `narumi_server` パッケージ（契約の写像なのでサーバー側が持つ）。エントリポイント `narumi = narumi_server.cli_tools:main`。従来のライブラリ直叩き CLI は `narumi-dev = narumi.cli:main` に改名
- サブコマンドは契約から自動生成: ツール名の `_` を `-` にしたコマンド（例 `narumi list-meetings --scope cloudnative --limit 10`）。オプションは `inputSchema.properties` から型付きで生成（string / integer / number / boolean はそのまま、array / object は JSON 文字列）。`request_id` は省略時に自動発番。汎用 `narumi tool <name> --json '{...}'` も用意
- 接続: `--server-url`（既定 `NARUMI_SERVER_URL` → `http://127.0.0.1:8765/mcp`）に `get_server_info` が応答すればそこへ MCP（Streamable HTTP）で送る。応答が無ければ in-process（`narumi_server.app.dispatch`）で実行。`--in-process` / `--require-server` で強制。録画系（`start_recording` / `stop_recording` / `get_recording_status`）は in-process では `busy`/`invalid_argument` 相当のエラーで拒否（プロセスが終わると録画が止まるため）
- 出力は JSON（`--pretty` 既定、`--raw` で 1 行）。エラーは error_envelope をそのまま stderr に出し exit 2
- テスト: 契約の全ツールにサブコマンドがあること、オプション生成がスキーマと一致すること、in-process 実行の代表ケース、HTTP 経由の代表ケース（テスト内で起動したサーバーに接続）

## パリティ検査
- 契約 ↔ CLI: 自動生成なので構造的に一致。テストで確認
- アプリ ⊆ 契約: `NarumiMenuBarCore` にアプリが使うツール名の一覧（`ToolCatalog`）を持ち、XCTest で `contracts/manifest.json` の tools に含まれることを検査
- アプリ ⊇ 契約: 上表を正とし、アプリ本体ウィンドウ実装時に「状態」列を埋める。全行が「実装済み」になるまでアプリは未完成と扱う

## 実装順
1. 契約拡張（この文書のツール群）＋サーバー実装＋カタログ検索＋プロファイル＋キャンセル — 実装済み（2026-08-27。契約は 24 ツール、`server/tests/test_surface_tools.py` / `test_profiles_tools.py` / `test_cancel.py` / `test_e2e_server.py` で検証）
2. `narumi` CLI の契約駆動化と `narumi-dev` 分離、パリティテスト — 実装済み（`narumi_server.cli_tools` + `server/tests/test_cli_tools.py`。契約 ↔ CLI の構造一致をテストで検査。アプリ ⊆ 契約の Swift 側検査は 4. の範囲）
3. 配布・自動更新（別文書）
4. アプリ本体ウィンドウ（上表の全行）— 実装済み（2026-08-27。`app/Sources/NarumiMenuBar/Views/` + `MainWindowModel` / `NarumiClient`。純粋ロジックとツール名一覧は `NarumiMenuBarCore`（`ContractModels` / `Formatting` / `MarkdownBlocks` / `ToolCatalog`）で、`ContractModelsTests` が契約の examples.output をそのままデコードして検査、`ToolCatalogTests` がアプリ ⊆ 契約を検査）
