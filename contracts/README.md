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
├── defs/provider_models.json      # モデル・能力・料金
└── tools/<name>.json    # 1 ツール 1 ファイル。ファイル名 = "name"
```

- `manifest.json` の `tools` はツール名の配列。`tools/<name>.json` と **過不足なく一致** させる（一致しなければローダーが起動時に `contract_mismatch` を投げる）
- `manifest.json` の `defs` は共通定義ファイルの相対パス配列。定義名は全ファイルを通して一意

## v2 開発版ツール一覧（37）

契約 2.0.0 は未公開の次期版。既存の会議・プロファイル書込形はこの段階では維持する。
工程別モデル選択・複数生成は後続段階で追加し、実装済みの範囲は
`get_server_info.capabilities.workflow` で判定する。プロバイダ名の存在だけでは判定しない。

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

プロバイダ型は別の defs に分ける。PR1 の接続対象は Anthropic API / Claude Agent SDK / Ollama。
Claude SDK は API キー認証のみ。未承認のサブスク認証や未実装の Codex / OpenAI 接続は受け付けない。
モデル一覧の取得成功と生成可能性は区別し、不明な能力・上限・単価を推測で補わない。

### 接続・秘密・状態の扱い

- 新規接続は `provider_id / display_name / auth_method` を指定する。更新は `connection_id / expected_revision` を指定し、種別変更は受け付けない。
- `api_key` は write-only。省略は保持、null は削除。新規のキー未設定接続は保存できるが、生成利用は不可。
- CLI の秘密入力は非表示プロンプトか stdin を使う。通常の argv・文字列 `--json`・ログ・応答・要求キャッシュへ秘密を出さない。
- 接続の無効化は資格情報を保持し、再有効化で認証済み状態へ自動復帰しない。メタデータの観測だけでは設定版を増やさない。
- 認証操作は `start_request_id` でも照会できる。応答喪失・再起動時に不明な操作を成功扱いせず、自動でログインを再開始しない。
- runtime 準備はカタログの固定 ID だけを受け付ける。`provider_setup` job を返し、進行中・直近の受付は `list_providers` でも参照できる。

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
| 必須入力の追加、出力キーの削除・型変更、enum 値の削除、ツールの削除・改名、エラーコードの削除 | major |
| 任意入力の追加、出力キーの追加、ツールの追加、enum 値の追加、エラーコードの追加 | minor |
| `description` / `title` / `examples` のみ | patch |

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
