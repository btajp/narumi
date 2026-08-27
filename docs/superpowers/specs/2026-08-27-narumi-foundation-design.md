# narumi 基盤設計（2026-08-27）

Notion「議事録生成システム」の確定事項をリポジトリの具体構造に落とした設計メモ。対象は実装ステップ Step 1（録画 MVP）〜 Step 2（ffmpeg 分離＋Whisper → プレーン議事録）を縦に貫く基盤。Step 3 以降（外部コンテキスト突合・キースライド・Vision・Notion エクスポート・アップグレード再生成の本実装）は「拡張できる形」を用意するに留める。

## 1. ディレクトリ構成

```
narumi/
├── AGENTS.md                 # 開発ルール（CLAUDE.md はシンボリックリンク）
├── pyproject.toml            # uv ワークスペース（.venv はここ）
├── contracts/                # MCP ツール契約（正本）
│   ├── manifest.json         # contract_version, tools 一覧
│   ├── defs/common.json      # 共通型（scope / error / job / meeting_id / request_id …）
│   └── tools/<tool>.json     # 1 ツール 1 ファイル（name / description / annotations / inputSchema / outputSchema）
├── pipeline/                 # Python パッケージ narumi
│   ├── pyproject.toml
│   ├── src/narumi/
│   │   ├── models.py         # 共有データモデル（Segment / Transcript / Turn / SpeakerMap / Alignment …）
│   │   ├── errors.py         # NarumiError + 構造化エラーコード
│   │   ├── config.py         # データルート（NARUMI_HOME）と既定値
│   │   ├── bundle/           # セッションバンドル・manifest・冪等ステージ実行
│   │   ├── catalog/          # narumi.db（再構築可能な索引）＋監査ログ
│   │   ├── contracts/        # 契約ローダー（$ref 解決・ツール列挙）
│   │   ├── preprocess/       # ffmpeg 分離（16kHz mono wav / フレーム抽出）
│   │   ├── transcribe/       # 文字起こしエンジン抽象化（fake / mlx-whisper / faster-whisper）
│   │   ├── diarize/          # 話者分離（layer1 tracks / fake / pyannote）
│   │   ├── align/            # 第 1 段: 決定的アライメント（区間対応表）
│   │   ├── llm/              # LLM プロバイダ抽象化＋能力プロファイル＋送信ポリシー
│   │   ├── generate/         # 第 2 段（区間統合）＋議事録生成（plain / LLM）
│   │   ├── export/           # プラグイン型エクスポーター（markdown / html）
│   │   ├── pipeline.py       # 工程オーケストレーター（process / regenerate / export）
│   │   └── cli.py            # dev CLI `narumi-dev`
│   └── tests/
├── server/                   # Python パッケージ narumi_server（MCP サーバー）
│   ├── pyproject.toml
│   ├── src/narumi_server/
│   │   ├── app.py            # 契約からツール登録・入力検証・ディスパッチ
│   │   ├── handlers/         # ツール名 → ハンドラ
│   │   ├── jobs.py           # ジョブ管理（カタログ永続化）
│   │   ├── recording.py      # narumi-recorder サブプロセス制御
│   │   ├── transports.py     # stdio / Streamable HTTP
│   │   └── cli.py            # `narumi-server`
│   └── tests/
├── app/                      # Swift Package
│   ├── Package.swift
│   ├── Sources/NarumiRecorderKit/   # ScreenCaptureKit キャプチャ → ファイル（ライブラリ）
│   ├── Sources/narumi-recorder/     # 録画 CLI ヘルパー（server が起動）
│   ├── Sources/NarumiMenuBar/       # メニューバーアプリ `narumi.app`（MCP クライアント）
│   └── Tests/
└── scripts/                  # dev.sh / gen-types.sh / build-app.sh
```

## 2. データルートとセッションバンドル

- データルート: `NARUMI_HOME`（既定 `~/Library/Application Support/narumi`）。配下に `meetings/<meeting_id>/`（バンドル）と `narumi.db`
- `meeting_id`: `YYYYMMDDTHHMMSSZ-<8 hex>`（録画開始 UTC ＋乱数）。パターンは契約 defs で共有
- バンドル構成（ファイルが正本）:

```
meetings/<meeting_id>/
├── manifest.json
├── tracks/         # 原録音・録画（破棄可）: screen.mp4 / mic.m4a / system.m4a / recorder.json
├── preprocess/     # ffmpeg 派生物（破棄可・再生成可）: mic.16k.wav / system.16k.wav / frames/ / slides.json / slides/
├── transcripts/    # 系統別: own-mic.json / own-system.json / ext-<context_id>.json
├── diarization/    # 層別: layer1-tracks.json / layer2-<engine>.json / layer3-screen.json / layer4-external.json
├── merged/         # alignment.json（区間対応表）/ merged.json（統合済みセグメント。speaker_map を内包）/ speaker_map.json（便宜コピー）/ integrate_cache.json（区間キャッシュ。どちらも artifact ではない）
├── minutes/        # v1/minutes.md, v1/meta.json, v1/slides/ … 版は増えるだけ（上書き禁止）
├── context/        # brief.json / sources/<context_id>.json（register_context で受けた原文）
└── logs/           # ジョブログ
```

### manifest.json

```jsonc
{
  "manifest_version": 1,
  "meeting_id": "20260827T030500Z-a1b2c3d4",
  "meeting_name": "…", "engagement": null, "scope": null, "profile": "default",
  "created_at": "…", "updated_at": "…",
  "recording": {
    "started_at": "…", "stopped_at": "…", "duration_sec": 0.0,
    "tracks": { "screen": {"path": "tracks/screen.mp4", "sha256": "…", "bytes": 0, "discarded": false}, "mic": {…}, "system": {…} }
  },
  "config": {
    "transcription_engine": "auto", "diarization_engine": "none", "llm_provider": "none",
    "external_send_policy": "local_only", "language": "ja", "self_name": null, "vocab_hints": []
  },
  "artifacts": {
    "preprocess/audio/mic": {
      "path": "preprocess/mic.16k.wav", "sha256": "…",
      "inputs": {"tracks/mic": "<sha256>"}, "params": {"sample_rate": 16000, "channels": 1}, "params_hash": "…",
      "producer": {"name": "ffmpeg", "version": "7.1"}, "created_at": "…"
    }
  },
  "contexts": [ {"context_id": "ctx-…", "source_type": "text", "registered_at": "…", "path": "context/sources/ctx-….json", "status": "stored"} ],
  "minutes_versions": [ {"version": 1, "path": "minutes/v1/minutes.md", "generated_at": "…", "provider": "none"} ],
  "exports": [ {"destination": "markdown", "ref": "/path/out.md", "minutes_version": 1, "at": "…"} ],
  "regenerations": [ {"job_id": "…", "at": "…", "reason": "regenerate", "minutes_version": 2} ]
}
```

### 冪等ステージ実行

`narumi.bundle.Bundle.run_stage(key, inputs, params, producer, fn)`:
1. `inputs` = 上流成果物キー → sha256 の dict、`params_hash` = params の正規化 JSON の sha256
2. manifest の `artifacts[key]` が同じ `inputs` と `params_hash` を持ち、`path` が存在すれば **スキップ**（`force=True` で強制）
3. 実行後に成果物の sha256 を計算して記録する。成果物のハッシュが次工程の `inputs` になる

録画破棄 = `tracks/*` を削除し `discarded: true`。破棄後は `preprocess/` 以降の成果物だけで再生成できる（`tracks` を inputs に持つ工程は既存成果物のスキップ判定に必要な情報を manifest に残しているので、再実行を要求されない限り成立する）。

## 3. 共有データモデル（`narumi/models.py`）

- `Segment { id, start, end, text, speaker?, confidence?, words? }`（秒。`id` は `<source_id>:<index>`）
- `Transcript { source_id, kind: own|external, track: mic|system|null, engine: {name, version, params}, language, time_offset, segments }`
- `Turn { start, end, speaker, confidence, layer: 1..4 }` / `Diarization { layer, engine, turns }`
- `SpeakerEntry { name?, confidence, evidence: [{layer, detail}] }` / `SpeakerMap { speakers: {label: SpeakerEntry} }`
- `Interval { id, start, end, columns: {source_id: [segment_id]} }` / `Alignment { intervals, offsets: {source_id: sec}, anchors: [...] }`
- `MergedSegment { id, start, end, text, speaker_label, speaker_name?, sources: [segment_id] }`
- `MeetingConfig`（manifest.config と同型）、`ExternalSendPolicy = local_only | subscription_ok | api_ok`

話者ラベルの約束: 第 1 層は `me`（マイク）/ `other`（システム音声、単一話者扱い）。第 2 層以降は `SPEAKER_00` 形式の匿名ラベル。実名は `SpeakerMap` で解決し、`merged.json` の `speaker_name` に入る。実名化できなかった話者は `speaker_name = null` のまま議事録に「話者不明」として明示する。

## 4. 工程と担当

| 工程 | 決定的 / LLM | 出力 | Step |
|---|---|---|---|
| preprocess | 決定的（ffmpeg） | `preprocess/*.16k.wav`（key `preprocess/audio/{mic,system}`） | 2 |
| brief | 決定的（gaia 照会は `NARUMI_GAIA_URL` 設定時のみ） | `context/brief.json`（key `context/brief`。inputs は設定サブセット＋登録済みコンテキスト原文のハッシュ） | 6 |
| transcribe | 決定的（固定パラメータ） | `transcripts/own-{mic,system}.json`（key `transcripts/own-{mic,system}`。vocab_hints はブリーフのマージ済み語彙） | 2 |
| context parse | 決定的（`register_context` 時に即時実行） | `transcripts/ext-<context_id>.json`（key `transcripts/ext-<context_id>`。WebVTT / SRT / Zoom txt / プレーン） | 3 |
| diarize layer1 | 決定的 | `diarization/layer1-tracks.json`（key `diarization/layer1`） | 2 |
| diarize layer2 | 決定的（pyannote 等） | `diarization/layer2-<engine>.json`（key は `diarization/layer2` 固定。エンジン名は params に記録し、エンジンを替えると同 key を差し替える） | 2（抽象化のみ・既定 none） |
| diarize layer4 | 決定的 | `diarization/layer4-external.json`（key `diarization/layer4`。ext トランスクリプトの話者名。ext が無ければ artifact 無しでスキップ） | 3 |
| slides | 決定的（ffmpeg フレーム抽出 + pHash） | `preprocess/slides.json` + `preprocess/slides/*.png`（key `preprocess/slides`。画面トラックが無ければ artifact 無しでスキップ） | 4 |
| align（第 1 段） | 決定的 | `merged/alignment.json`（key `merged/alignment`。own / ext 全系統） | 2 |
| diarize layer3 | LLM（vision） | `diarization/layer3-screen.json` + `layer3-names.json`（key `diarization/layer3`。vision 対応プロバイダが送信ポリシーで許可されたときだけ。違反は policy_violation、非対応は artifact 無しでスキップ） | 5 |
| integrate（第 2 段） | LLM（区間ごと。単一系統なら素通し） | `merged/merged.json`（key `merged/merged`。`merged/speaker_map.json` は便宜コピー、`merged/integrate_cache.json` は区間キャッシュでどちらも artifact ではない） | 2 / 8 |
| generate | LLM or plain | `minutes/vN/`（key `minutes/vN`。入力・パラメータが同じなら新版を作らない。キースライド画像を `minutes/vN/slides/` に複製して埋め込み、ブリーフをプロンプトに注入） | 2 / 4 / 6 |
| export | 決定的 | 出力先ごと | 2（markdown / html）/ 6（notion / gaia-library） |

オーケストレーターは `narumi.pipeline`（`process_meeting` / `regenerate_meeting` / `refresh_meeting` / `export_meeting`）。`process` は preprocess → brief → transcribe → diarize（layer1/2/4）→ slides → align → layer3 → integrate → generate の順に全工程を実行し、`regenerate` は align → integrate → generate だけを呼ぶ（それ以外は呼ばない。文字起こしの無い会議への `regenerate` は `not_found`）。MCP ツール `regenerate` が呼ぶ `refresh_meeting` は、上流工程を冪等に通した上で（未実行・失敗済み・`set_meeting_config` や `register_context` で inputs / params が変わった工程だけが実際に走る。`force` でも強制しない）align 以降を再実行する。いずれも `manifest.status` を `processing` → `ready` / `failed` に更新し、`regenerate` / `refresh` は `manifest.regenerations` に記録を追記する。カタログ更新は pipeline の責務ではなく、server ハンドラ / dev CLI が完了後に行う。`diarization_engine` を `none` に戻したときは `diarization/layer2` の記録とファイルを取り除く（integrate の inputs が変わり再統合される）。

## 5. エンジン / プロバイダ / エクスポーターの抽象

- `TranscriptionEngine`: `name / version / profile{sends_audio_externally, supports_vocab_hints} / transcribe(wav, language, vocab_hints) -> list[Segment]`。レジストリ名: `fake` / `mlx-whisper` / `faster-whisper` / `auto`（mlx → faster の順で import 可能なもの）
- `DiarizationEngine`: `name / profile / diarize(wav) -> list[Turn]`。レジストリ名: `none` / `fake` / `pyannote`（HF トークン必須。無ければ構造化エラー）
- `LLMProvider`: `name / profile: CapabilityProfile{vision, context_window, cost_class: local|subscription|api, data_destination, tool_use} / complete(prompt, system?, images?) -> str`。レジストリ名: `none` / `fake` / `claude-agent-sdk` / `anthropic-api` / `ollama`
- 送信ポリシー: `external_send_policy` と `data_destination` を突き合わせ、違反は `policy_violation` エラー（黙ってフォールバックしない）
  - `local_only` → data_destination == local のみ
  - `subscription_ok` → local ＋ cost_class == subscription
  - `api_ok` → すべて
- `Exporter`: `name / describe() / export(bundle, minutes_version, options) -> ExportResult{destination, ref, at}`。レジストリ名: `markdown` / `html` / `notion`（ページ作成＋Markdown→ブロック変換。スライド画像のアップロードは未対応でローカル参照の callout を置く）/ `gaia-library`（`propose_update` の提案キューのみ）。ファイル系の `options` は `output_path`（絶対パス）と `overwrite`（既定 false: 既存ファイル・`<stem>-slides` は上書きしない。既定出力先 `<NARUMI_HOME>/exports/` は narumi 管理なので置き換える）。server は `options_schema` で検証してから呼ぶ

## 6. 契約（`contracts/`）

- ツールファイル: `{"name", "title", "description", "annotations": {"readOnlyHint", "destructiveHint", "idempotentHint"}, "inputSchema", "outputSchema"}`。スキーマは JSON Schema 2020-12。共通型は `../defs/common.json#/$defs/<name>` を `$ref`
- ローダー（`narumi.contracts`）は起動時に全ツールを読み、外部 `$ref` をインライン化して自己完結スキーマにする。`manifest.json` の `tools` 一覧とファイルの過不足、ハンドラの過不足は起動時エラー
- 書き込み系ツールは `request_id`（クライアント発番、冪等キー）を受ける。読み取り系は `readOnlyHint: true`
- エラーは `{"error": {"code", "message", "details?"}}` を構造化コンテンツで返し `isError: true`。コード: `not_found / scope_denied / contract_mismatch / busy / invalid_argument / policy_violation / recorder_unavailable / engine_unavailable / internal`
- v1 ツール: `get_server_info / start_recording / stop_recording / list_meetings / get_meeting / get_transcript / register_context / regenerate / set_meeting_config / export_minutes / list_export_destinations / get_job_status`
- scope: `string | string[]`。省略時は scope 未設定の会議のみ（default deny）。配列で複数指定した場合のみ横断し、カタログの `audit_log` に記録

## 7. MCP サーバー

- `mcp` 公式 Python SDK の低レベル `Server` を使い、`list_tools` は契約から、`call_tool` は jsonschema 検証 → ハンドラ → 出力検証（テスト時）で返す
- トランスポート: `--stdio`（dev / テスト）、`--http --port 8765`（Streamable HTTP、常駐）。`--http` は SIGTERM でも必ず `ctx.close()` に到達する: uvicorn は graceful shutdown の後に捕捉した SIGTERM を再送出するため、`transports.graceful_sigterm` で最初の SIGTERM を `ShutdownRequested` 例外に変え（2 回目以降は無視）、`finally` の `ctx.close()` を通す。uvicorn の graceful shutdown は開いたままの接続（MCP クライアントの GET ストリーム）を 10 秒で打ち切る
- ジョブ: `JobManager`（ThreadPoolExecutor、既定 1 ワーカー）。`jobs` テーブルに `queued / running / succeeded / failed` を永続化。`process / regenerate / export` はジョブ化。1 会議につき同時に 1 ジョブ（`submit` 内で `busy`）
- manifest の書き込みは会議ごとのロック（`narumi_server.locks.MeetingLocks`）で直列化する。ジョブは実行中ずっと保持し、書き込み系ハンドラ（`register_context` / `set_meeting_config` / `export_minutes` / `stop_recording`）はロック下で読み直してから保存する。ジョブ実行中の書き込みは `busy`（黙って上書き・巻き戻しはしない）
- 書き込み系ツール（`regenerate` / `register_context` / `set_meeting_config` / `export_minutes`）も読み取り系と同じ `scope` セレクタ（default deny）を受ける。`set_meeting_config` の scope 変更は `new_scope` で行い、現在の scope をセレクタで覆っていることが条件
- 録画制御: `RecordingController` が `narumi-recorder record --output <bundle>/tracks` を起動。stdout の JSON Lines イベント（`started / stopped / error`）を読み、`stop` は SIGINT。バイナリは `NARUMI_RECORDER` env → `app/.build/{release,debug}/narumi-recorder` の順で探し、無ければ `recorder_unavailable`。同時録画は 1 本（`busy`）。`stop_recording` は既定で `process` ジョブを自動投入
- サーバー側の認可（human / agent ロール）は narumi v1 に承認系ツールが無いため未実装。gaia-library 側と揃える時点で追加する

## 8. 録画アプリ（`app/`）

- `narumi-recorder`（CLI）: `record --output <dir> [--display <id>] [--no-video] [--mic <device-uid>]` / `check`（権限状態）/ `list-displays`。SCStream で `screen`・`audio`（システム音声、`excludesCurrentProcessAudio`）・`microphone`（macOS 15+ `captureMicrophone`）を別々の出力として受け、AVAssetWriter で `screen.mp4`（H.264）/ `system.m4a`（AAC）/ `mic.m4a`（AAC）に**別ファイル**で書く。開始・停止時刻と各ファイルの実尺を `recorder.json` に残す（`stopped` の内容 + `started_at` + `recorder_version`。失敗時は `error` オブジェクト）。stdin がパイプで EOF になった場合（親プロセス消滅）も finalize する。1 フレームも取れなかった `screen` は `bytes: 0` でファイル無しとして報告し、server はそのトラックを manifest から落とす
- SIGINT / SIGTERM / stdin の `stop` 行で finalize。イベントは stdout に JSON Lines。録画途中でキャプチャが失敗しても音声トラックを finalize できた場合は `stopped` を出してから `error` を出す（server は会議を `recorded` にし、`recording.recorder.error` に失敗を残す）。server 終了時は `stop_recording` 相当で finalize し、SIGKILL は最後の手段
- `NarumiMenuBar`: NSStatusItem。「録画開始 / 停止」「会議名入力」「サーバー状態」「サーバーを再起動」「リポジトリを選択…」「ログを開く」。MCP Streamable HTTP（`http://127.0.0.1:8765/mcp`）へ `initialize` → `tools/call` を行う最小 JSON-RPC クライアントを同梱。表示名「narumi」
- サーバープロセスのランチャー: `narumi.app` は `narumi-server` の**プロセス**を自分で起動・停止する（Terminal 不要）。データ操作は引き続きすべて MCP ツール経由で、ランチャーはバンドル・カタログ・録画ファイルに触れない。純粋ロジックは Foundation のみの `NarumiMenuBarCore`（`ServerConfig` = リポジトリ解決 `NARUMI_REPO` → UserDefaults `narumi.repoPath` → `<repo>/dist/narumi.app` ヒューリスティック（`pyproject.toml` と `server/pyproject.toml` の両方が必要）→ 未設定、ポート `NARUMI_SERVER_PORT`（既定 8765）と URL（`NARUMI_SERVER_URL` 無指定時はポートから導出）、同梱 recorder、ログ `~/Library/Logs/narumi/server.log`、`NARUMI_HOME` 引き継ぎ / `ServerCommand` = `/bin/zsh -lc 'exec uv run --project "$1" narumi-server --http --host 127.0.0.1 --port "$2" [--recorder "$3"]' narumi-server <repo> <port> [<recorder>]`。パスは位置パラメータ（argv）で渡し、シェル文字列に埋め込まない（環境変数渡しだとログインシェルの `~/.zshenv` / `~/.zprofile` の `export NARUMI_*` に黙って上書きされる） / `ServerState`）、プロセス制御は AppKit 側の `ServerLauncher`（`Foundation.Process`、stdout/stderr をログへ追記、5 MiB 超で切り詰め）
  - 状態: `notConfigured`（リポジトリ未解決）/ `external(url)`（起動前の `get_server_info` に応答があった。自前サーバーを尊重し、起動も停止もしない）/ `starting(since)`（1 秒間隔で最大 30 秒 `get_server_info` を待つ）/ `running(pid)` / `stopped(exitCode)` / `failed(message)`（起動不可、起動時の異常終了、30 秒タイムアウト。タイムアウト時に生きているプロセスは残す）
  - 終了（⌘Q / SIGTERM / SIGINT）: このクライアントが開始した録画があれば確認のうえ `stop_recording` を呼ぶ（直後に停止する自前サーバーには `auto_process: false` — 停止するサーバーに積んだ process ジョブは完走できない。外部サーバーなら既定の true）。自前で起動したサーバーには SIGTERM → 最大 60 秒待機（uvicorn の graceful shutdown 10 秒 + recorder の停止タイムアウト 30 秒 + 終了猶予 10 秒 + ハッシュ計算。server は `ServerContext.close` で録画を確定する）→ プロセスグループごと SIGKILL。`ServerContext.close` は録画確定 → ジョブ停止 → カタログ close の順（長いジョブ待ちで録画確定が SIGKILL に巻き込まれないように）
- 配布時の署名・公証・`.app` 化は `scripts/build-app.sh` で最小 Info.plist（`LSUIElement`, `NSMicrophoneUsageDescription`, `NSScreenCaptureUsageDescription`）を付けてバンドルする

## 9. カタログ（`narumi.db`）

```sql
meetings(meeting_id PK, meeting_name, engagement, scope, status, started_at, stopped_at, bundle_path, latest_minutes_version, updated_at)
jobs(job_id PK, meeting_id, kind, status, progress, result_json, error_json, created_at, updated_at)
exports(id PK, meeting_id, destination, ref, minutes_version, at)
contexts(context_id PK, meeting_id, source_type, status, registered_at)
audit_log(id PK, actor, action, detail_json, at)
segments_fts(meeting_id, source_id, segment_id, start, end, speaker, text)  -- FTS5 trigram
```

`rebuild()` は `meetings/*/manifest.json` と `merged/merged.json` を走査して派生テーブル（meetings / exports / contexts / segments_fts）を再構成する。jobs（揮発）・requests（冪等キーの再送キャッシュ）・audit_log（追記専用の履歴。バンドルからは導出できない）は再構築対象外で、そのまま残す。

## 10. テスト方針

- 実エンジン・実プロバイダ・実録画に依存しない。`fake` 実装と ffmpeg 合成音声（`-f lavfi -i sine`）で全工程を通す
- 契約テスト: 全スキーマが 2020-12 として妥当、manifest とファイルの一致、各ツールにハンドラがある、サンプル入力が検証を通る
- サーバーテスト: `mcp` の in-memory クライアントで `list_tools / call_tool`。録画は Python 製 fake recorder（`tests/fake_recorder.py`）を `NARUMI_RECORDER` に指定
- 実エンジン smoke（`-m real`）は opt-in

## 11. 範囲外として残っているもの（拡張点は確保済み）

Step 3〜8（外部トランスクリプト突合・キースライド・第 3/4 層話者判定・会議ブリーフ v1・Notion / gaia-library エクスポーター・影響区間だけの再統合）は実装済み。残りは:

- `url` コンテキストの取得: `register_context` は URL を参照として保存するだけ（`status: stored`）。取得は送信ポリシーを通すフェッチ工程として別途設計する
- gaia-library サーバー本体（別リポジトリ・未実装）: クライアント（`narumi.gaia`）・ブリーフ照会・エクスポーターは実装済みでフェイクサーバー検証のみ。実契約が固まったらツール名・キー名を突き合わせて締める
- Notion エクスポートのスライド画像アップロード（多段の file upload フロー。現状はローカル画像の場所を callout で案内）
- コンテキスト注入 v2（tool_use 可プロバイダ限定のエージェント型プル。v1 のブリーフに追記保存して冪等性を回復する形）
- 実 vision プロバイダでの layer3 実機確認（テストはフェイク vision プロバイダ）
- human / agent ロールの API キー
