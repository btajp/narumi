# contracts/ — narumi MCP ツール契約

契約が正本。ツールの追加・変更は **契約 → テスト → 実装** の順で行う（AGENTS.md）。
サーバー（`narumi_server`）は起動時にここを読み込み、`tools/list` の内容と `tools/call` の入出力検証をこのファイル群から組み立てる。

## ファイル構成

```
contracts/
├── manifest.json        # name / contract_version (semver) / defs / tools
├── defs/common.json     # 共通型（$defs）
└── tools/<name>.json    # 1 ツール 1 ファイル。ファイル名 = "name"
```

- `manifest.json` の `tools` はツール名の配列。`tools/<name>.json` と **過不足なく一致** させる（一致しなければローダーが起動時に `contract_mismatch` を投げる）
- `manifest.json` の `defs` は共通定義ファイルの相対パス配列。定義名は全ファイルを通して一意

## v1 ツール一覧（24）

アプリ（narumi.app）の全操作をカバーする app-first のツールセット（`docs/superpowers/specs/2026-08-27-narumi-surface-parity-design.md`）。

| 分類 | ツール |
|---|---|
| サーバー | `get_server_info`（capabilities + diagnostics） |
| 録画 | `start_recording` / `stop_recording` / `get_recording_status` / `import_recording` |
| 会議の閲覧 | `list_meetings`（`active_job` 付き）/ `search_transcripts` / `get_meeting` / `get_transcript` / `get_minutes` |
| 会議の変更 | `register_context` / `regenerate` / `set_meeting_config` / `discard_tracks` / `delete_meeting` |
| エクスポート | `export_minutes` / `list_export_destinations` |
| ジョブ | `get_job_status` / `cancel_job` |
| プロファイル | `list_profiles` / `get_profile` / `set_profile` / `delete_profile` |
| カタログ | `rebuild_catalog` |

`defs/common.json` の主な共通型: `meeting_id` `request_id` `job_id` `context_id` `scope_name` `scope` `timestamp` `external_send_policy` `job_status` `job_kind` `job` `error_code`（`cancelled` を含む）`error` `error_envelope` `meeting_config` `track_status` `meeting_summary`（任意の `active_job` 付き）`segment` `context_source_type` `export_destination` `profile`。

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
- `description` には、既定値、相互排他（`oneOf`）、`scope` の意味、返しうるエラーコードを書く。すべてのツールは `error_envelope`（`{"error": {"code", "message", "details?"}}`、`isError=true`）を返しうるので、それも明記する
- `examples.input` は 2 件以上、`examples.output` は 1 件以上。すべて対応するスキーマで検証を通ること（テストで強制）

## `$ref` 規約

- 共通型は `{"$ref": "../defs/common.json#/$defs/<name>"}`（`tools/` からの相対パス）で参照する。`$ref` の隣に `description` を置いてよい（2020-12 では並記が有効）
- `defs/common.json` 内部の相互参照は `#/$defs/<name>`。defs から他ファイルを参照してはいけない
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
