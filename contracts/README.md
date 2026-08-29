# contracts/ — narumi MCP ツール契約

契約が正本。ツールの追加・変更は **契約 → テスト → 実装** の順で行う（AGENTS.md）。
サーバー（`narumi_server`）は起動時にここを読み込み、`tools/list` の内容と `tools/call` の入出力検証をこのファイル群から組み立てる。

## ファイル構成

```
contracts/
├── manifest.json        # name / contract_version (semver) / defs / tools
├── defs/common.json     # 会議・ジョブ・エラーの共通型（$defs）
├── defs/providers.json  # プロバイダ・runtime・対応機能
├── defs/provider_connections.json # 接続・認証操作
├── defs/provider_models.json      # モデル・能力・料金・議事録モデル選択
└── tools/<name>.json    # 1 ツール 1 ファイル。ファイル名 = "name"
```

- `manifest.json` の `tools` はツール名の配列。`tools/<name>.json` と **過不足なく一致** させる（一致しなければローダーが起動時に `contract_mismatch` を投げる）
- `manifest.json` の `defs` は共通定義ファイルの相対パス配列。定義名は全ファイルを通して一意

## v4 ツール一覧（37）

契約 4.0.0 は OpenAI API 接続と、4 系統のテキスト議事録モデル選択を扱う。
既存の `llm_provider` は維持し、文字起こし・発話統合・画像処理へ選択を暗黙に適用しない。
`get_server_info.capabilities.minutes_model_providers` は議事録生成を実装した adapter の一覧。
この一覧とは別に、接続の認証・準備状態と各モデルの能力を確認する。
全工程のモデル選択、外部 API の文字起こし、複数生成・統合、Claude Agent SDK による明示的な議事録モデル選択は今回の対象外。

| 分類 | ツール |
|---|---|
| サーバー | `get_server_info`（capabilities + diagnostics） |
| 録画権限 | `configure_recording_permission`（許可要求 / 設定を開く。録画しない） |
| 録画 | `start_recording` / `stop_recording` / `get_recording_status` / `import_recording` |
| 会議の閲覧 | `list_meetings`（`active_job` 付き）/ `search_transcripts` / `get_meeting` / `get_transcript` / `get_minutes` |
| 会議の変更 | `register_context` / `regenerate` / `set_meeting_config` / `discard_tracks` / `delete_meeting` |
| エクスポート | `export_minutes` / `list_export_destinations` |
| ジョブ | `get_job_status` / `cancel_job` |
| プロファイル | `list_profiles` / `get_profile` / `set_profile` / `delete_profile` |
| カタログ | `rebuild_catalog` |
| Gaia 接続 | `get_gaia_connection` / `set_gaia_connection` / `test_gaia_connection` |
| プロバイダ接続 | `list_providers` / `list_provider_connections` / `set_provider_connection` / `delete_provider_connection` |
| プロバイダ準備・認証 | `prepare_provider_runtime` / `authenticate_provider_connection` / `get_provider_auth_status` |
| プロバイダメタデータ | `test_provider_connection` / `list_provider_models` |

権限設定と録画系ツールは常駐サーバーを必要とする。`get_server_info` は
`refresh_permissions: true` で権限を再確認できるが、読み取りから許可要求は行わない。
`permission_setup_in_progress` が true の間は録画開始とアプリの再起動・更新を保留する。
権限設定の同一 ID による処理中再送は待機せず `busy`、完了済み再送は元の結果を返す。
応答が不明な操作の完了確認には、操作元と同じ `server_instance_id` の fresh 応答を使う。
別 ID や ID 欠落の応答は元のヘルパーの終了証明として扱わない。

`defs/common.json` の主な共通型: `meeting_id` `request_id` `job_id` `context_id` `scope_name` `scope` `timestamp` `external_send_policy` `job_status` `job_kind` `job` `error_code`（`cancelled` を含む）`error` `error_envelope` `meeting_config` `track_status` `meeting_summary`（任意の `active_job` 付き）`segment` `context_source_type` `export_destination` `profile`。

プロバイダ型は別の defs に分ける。接続対象は Anthropic API / Claude Agent SDK / Codex App Server / Ollama / OpenAI API。
Claude SDK と OpenAI API は API キー認証のみ。OpenAI API の接続先は `https://api.openai.com` に固定する。
Codex は公式 ChatGPT 認証のみで、接続先は `https://chatgpt.com` に固定し、OpenAI API キーを流用しない。
Claude のサブスク認証と `minutes_model` による Claude SDK 生成は未対応。
モデル一覧の取得成功と生成可能性は区別し、不明な能力・上限・単価を推測で補わない。

### 接続・秘密・状態の扱い

- 新規接続は `provider_id / display_name / auth_method` を指定する。更新は `connection_id / expected_revision` を指定し、種別変更は受け付けない。
- API 接続の `api_key` は write-only。省略は保持、null は削除。新規のキー未設定接続は保存できるが、生成利用は不可。Codex 接続は null を含め `api_key` 自体を受け付けない。
- CLI の秘密入力は非表示プロンプトか stdin を使う。通常の argv・文字列 `--json`・ログ・応答・要求キャッシュへ秘密を出さない。
- 接続の無効化は資格情報を保持し、再有効化で認証済み状態へ自動復帰しない。メタデータの観測だけでは設定版を増やさない。
- 認証操作は `start_request_id` でも照会できる。応答喪失・再起動時に不明な操作を成功扱いせず、自動でログインを再開始しない。
- Codex は公式デバイスコード認証だけを使う。`authorization_url` は `https://auth.openai.com/codex/device` に固定し、`start` が `pending` の間だけ `user_code` と一対で返す。旧ブラウザ OAuth URL と loopback callback は受け付けない。
- `user_code` は 1〜32 文字の ASCII 英数字・ハイフンだけを許可する。URL とコードは進行中のメモリにだけ保持し、永続ログ・操作 receipt・診断へ残さない。完了・取消・失敗・不明・再起動時には両方 null にする。
- Codex の認証情報は接続ごとの narumi 専用 runtime 内に保持し、API 応答へ返さない。既存の Codex セッション・設定・認証情報は流用しない。
- runtime 準備はカタログの固定 ID だけを受け付ける。`provider_setup` job を返し、進行中・直近の受付は `list_providers` でも参照できる。

### テキスト議事録のモデル選択

`meeting_config.minutes_model` は nullable な選択オブジェクト。
`set_meeting_config` では省略時に現在値を保持し、null で解除する。
プロファイルの config、新しい会議の config、会議・プロファイルの応答にも同じ型を使う。
既存保存データにこの項目がない場合は null と同じ扱いで、従来の `llm_provider` の動作を維持する。

選択には対象の `provider`、`connection_id`、1 以上の `connection_revision`、
検証済みモデル一覧から明示的に選んだ `model_id` を指定する。
`parameters` は省略時 `{}`。プロバイダごとに次のキーだけを受け付ける。

| provider | 許可する parameters | 必要な外部送信ポリシー |
|---|---|---|
| `codex-app-server` | `reasoning_effort` | `subscription_ok` または `api_ok` |
| `openai-api` | `reasoning_effort`, `max_tokens` | `api_ok` |
| `anthropic-api` | `max_tokens` | `api_ok` |
| `ollama` | `max_tokens` | `local_only` 可。数値 loopback と検証済みローカルモデルのみ |

`reasoning_effort` は選択モデルの検証済み `parameter_schema` と照合する。
`max_tokens` は 1〜32768 の整数で、narumi が 1 要求の出力量を制限する値。
モデルの出力上限が既知の場合は、その上限を超える指定も拒否する。
省略時は adapter が既知のモデル上限と 4096 の小さい方を使い、モデル上限が不明ならアプリ側の制限として 4096 を使う。
このアプリ側制限をモデルの能力とは扱わず、未知の `descriptor.max_output_tokens` は null のままにする。
モデル能力を確認できない候補は表示できるが、`unverified` 等として選択を禁止する。
モデル自体が検証済みで出力上限だけが不明な場合は、アプリ側の要求制限を適用できる。
`availability_expires_on` は API が提供した終了日を `YYYY-MM-DD` のまま保持する任意項目。
未提供・不明の場合は省略または null とし、仮の期限・時刻・タイムゾーンを補わない。
終了日はモデルのキャッシュにも保持する。現在の UTC 日付が当日以降なら、取得時の状態が `available` でも選択・生成を拒否する。
これはアプリ側の保守的なルールであり、公式な終了時刻を示すものではない。
日付の形式と実在性は契約で検証し、現在日付との比較は実装側で行う。

`cache_epoch` は省略時 0、指定時は 0 以上の整数。
接続先・コマンド・認証情報・パスを生成パラメータに入れることはできない。

選択した接続の有効状態・版・認証・モデル能力と、更新後の外部送信ポリシーを保存時・実行時に検証する。
API 送信は `api_ok`、Codex 送信は `subscription_ok` または `api_ok` が必須。別プロバイダや別モデルへフォールバックしない。
同じ入力と選択では既存の議事録を再利用する。選択や `cache_epoch` だけを変更した場合は、
文字起こしや発話統合をやり直さず議事録生成だけを更新する。

`regenerate.force=true` は `minutes_model` が null の従来処理だけで使う。
明示的な `minutes_model` を選択した会議では `invalid_argument` で拒否する。
同じ内容で明示的に新しい生成試行を始める場合は、実行と送信先をユーザーが確認した後、
`set_meeting_config` で `minutes_model.cache_epoch` を明示的に増やして保存する。
その保存結果を `expected_config` に渡し、新しい `request_id` と `force=false` で `regenerate` を呼ぶ。
送信や結果の成否が不明な試行は自動再送しない。同じ選択のまま通常の再生成を呼んでも、
結果不明の記録を迂回して再送することはできない。

明示的なモデル選択がある会議の `regenerate` と、`auto_regenerate=true` の `register_context` は、
送信確認時に取得した設定全体を `expected_config` として渡す。
サーバーは会議のロック内で現設定と照合し、確認後に変更されていれば `configuration_conflict` で拒否する。
旧来の生成と、生成しないコンテキスト登録では省略できる。指定した場合はどちらでも照合する。

プロバイダ操作は認証済み常駐サーバーを使い、平文 HTTP や in-process へ降格しない。
`secure_transport` は要件の説明であり、証明書 pin や client token の信頼元ではない。
数値 loopback の TLS pin は所有者限定 bootstrap から取得し、照合後に Keychain の client token を送る。
他の MCP クライアントにも同じ認証または所有ユーザーの stdio ブリッジを使い、UI 専用権限を作らない。

## ツールファイルの形式

```jsonc
{
  "name": "get_meeting",                 // ファイル名と一致
  "title": "Get meeting",
  "description": "…",                   // LLM クライアント向けの正確な説明（後述）
  "annotations": {                       // 4 つとも bool 必須（MCP ToolAnnotations）
    "readOnlyHint": true, "destructiveHint": false, "idempotentHint": true, "openWorldHint": false
  },
  "inputSchema":  { "type": "object", "properties": {…}, "required": […], "additionalProperties": false },
  "outputSchema": { "type": "object", "properties": {…}, "required": […], "additionalProperties": false },
  "examples": { "input": [ {…}, {…} ], "output": [ {…} ] }
}
```

規約:

- スキーマは JSON Schema 2020-12。`inputSchema` / `outputSchema` は必ず `type: object`、`additionalProperties: false`、`required` を明示する
- 読み取り系ツールは `readOnlyHint: true` で `request_id` を持たない。書き込み系は `request_id`（クライアント発番の冪等キー）を **必須** にし、同じ `request_id` の再送は最初の結果を返す
- `test_provider_connection / list_provider_models` は認証・モデルメタデータの観測のみを行う読み取り系。観測キャッシュは更新できるが、設定変更・ログイン開始・会議送信・生成はしない。外部メタデータに接するツールは `openWorldHint: true` にする
- `description` には、既定値、相互排他（`oneOf`）、`scope` の意味、返しうるエラーコードを書く。すべてのツールは `error_envelope`（`{"error": {"code", "message", "details?"}}`、`isError=true`）を返しうるので、それも明記する
- `examples.input` は 2 件以上、`examples.output` は 1 件以上。すべて対応するスキーマで検証を通ること（テストで強制）

## `$ref` 規約

- 共通型は `{"$ref": "../defs/<file>.json#/$defs/<name>"}`（`tools/` からの相対パス）で参照する。ファイルは manifest.defs に列挙し、型はそのファイルで定義する。`$ref` の隣に `description` を置いてよい（2020-12 では並記が有効）
- defs 内部の相互参照は、ローダーが全ファイルをマージした共通名前空間への `#/$defs/<name>`。defs から他ファイルへの外部参照は使わない
- サポートするのはこの 2 形式だけ。絶対 URI や `$defs` 以外へのポインタはローダーが拒否する

## ローダーによるインライン化

`narumi.contracts.load_contracts(contracts_dir=None) -> ContractSet`（既定は `narumi.config.contracts_dir()`、`NARUMI_CONTRACTS_DIR` で上書き可）。

1. `manifest.json` を読み、`defs` の各ファイルの `$defs` をひとつの名前空間にマージする（重複名はエラー）
2. `manifest.tools` と `tools/*.json` の過不足、`name` とファイル名の不一致を検査する
3. 各ツールの `inputSchema` / `outputSchema` を深いコピーし、外部 `$ref` を `#/$defs/<name>` に書き換え、推移的に必要な定義をそのスキーマ自身の `$defs` にコピーする。結果は **自己完結スキーマ**（外部参照ゼロ）になり、そのまま MCP クライアントへ渡せる
4. `Draft202012Validator.check_schema` で妥当性を確認する。未解決 `$ref`、無効なスキーマ、annotations の欠落はすべて `ContractMismatchError`

`ContractSet` の主な API:

- `validate_input(tool, args)`: 違反は `InvalidArgumentError`（`details = {"tool", "errors": [{"path", "message", "validator"}]}`）
- `validate_output(tool, result)`: 違反は `ContractMismatchError`（サーバー側の実装不整合）
- `error_envelope_schema()` / `validate_error_envelope(payload)` / `schema_for_def(name)`
- `tools[name].tool_definition()`: MCP `Tool` と同じ camelCase の dict
- `format: date-time` は `rfc3339-validator` の有無に関係なく常に検証する（ローダーが独自チェッカーを登録）

## contract_version の上げ方

semver。`get_server_info` が返す `contract_version` でクライアントが互換性を判断する。

| 変更 | 上げる桁 |
|---|---|
| 必須入力の追加、出力キーの削除・型変更、enum 値の削除、閉じた出力 enum への値追加、ツールの削除・改名、エラーコードの削除 | major |
| 任意入力の追加、出力キーの追加、ツールの追加、enum 値の追加、エラーコードの追加 | minor |
| `description` / `title` / `examples` のみ | patch |

v3 では旧クライアントが受け付けない provider / auth enum 値と、認可 URL の文字列応答を追加したため major を上げる。
v4 は OpenAI API の provider 値、議事録の選択対象・パラメータ、必須の対応 provider 一覧を追加する。
旧 v3 Swift 型は Codex 以外の議事録選択を拒否するため major を上げ、v3 の保存済み Codex 設定と省略時の動作は維持する。

手順: `contracts/` を編集 → `manifest.json` の `contract_version` を更新 → `uv run pytest pipeline/tests/contracts` → 実装 → サーバーテスト。エラーコードを増やすときは `defs/common.json#/$defs/error_code` と `narumi.errors.ErrorCode` を同時に更新する（テストが一致を検査する）。

## examples とテスト

`pipeline/tests/contracts/test_contracts.py` が以下を検査する。

- manifest ↔ ファイルの一致、全スキーマの 2020-12 妥当性、必須キーと annotations
- 全 `examples.input` が `inputSchema` を、全 `examples.output` が `outputSchema` を通る
- `error_code` の enum が `narumi.errors.ErrorCode` と一致、共通型が `narumi.models` / `narumi.bundle.manifest` と一致
- ローダーが外部 `$ref` を残さない、`validate_input` が不正入力を `invalid_argument` で拒否する
- 読み取り系に `request_id` が無く、書き込み系では必須

サーバーテストは `ContractSet.tools[name].input_examples` を fake ハンドラへの入力として再利用できる。

## 型生成

```sh
scripts/gen-types.sh          # contracts/ → pipeline/src/narumi/contracts/_generated/
```

datamodel-code-generator（`--input-file-type mcp-tools`）でツールファイルから pydantic v2 モデルを生成する。生成物は gitignore 済みでコミットしない。手で編集せず、契約を直して再生成する。
