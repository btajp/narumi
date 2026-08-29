# contracts/ — narumi MCP ツール契約

**v6 正式契約。**
設定・更新入口・上限通知、実行履歴・成果物・公開版来歴の JSON I/F を定義する。
Schema、例、最小設定型の契約ゲートを通した後に、server・CLI・Swift の実装と統合検証を行う。

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
├── defs/transcription_models.json # API 音声認識の選択・再試行・結果不明情報
├── defs/minutes_ensemble.json     # 複数案と統合の設定・確認付き再試行・上限
├── defs/processing_runs.json      # 実行・node・call・attempt・artifact の不透明ID
├── defs/processing_provenance.json # current target・実行origin・安全なerror
├── defs/processing_run_records.json # run・node・一覧summary
├── defs/ensemble_documents.json   # evidence・claim・question・成果物本文
├── defs/processing_artifacts.json # artifact header・生成観測・reuse binding・公開来歴
└── tools/<name>.json    # 1 ツール 1 ファイル。ファイル名 = "name"
```

- `manifest.json` の `tools` はツール名の配列。`tools/<name>.json` と **過不足なく一致** させる（一致しなければローダーが起動時に `contract_mismatch` を投げる）
- `manifest.json` の `defs` は共通定義ファイルの相対パス配列。定義名は全ファイルを通して一意

## v6 ツール一覧（40）

契約 6.0.0 は、既存の単独議事録生成と API 音声認識を維持し、複数案の生成・統合と実行履歴の読み取りを追加する。
既存の `llm_provider` は維持し、文字起こし・発話統合・画像処理へ選択を暗黙に適用しない。
`get_server_info.capabilities.minutes_model_providers` は議事録生成を実装した adapter の一覧。
この一覧とは別に、接続の認証・準備状態と各モデルの能力を確認する。
`transcription_model_providers` は API 音声認識の対応経路で、常駐 HTTP サーバーは `["openai-api"]`、それ以外は `[]` を返す。
全工程のモデル選択、並列実行、画像送信、Claude Agent SDK による明示的な議事録モデル選択、既知話者の参照音声送信は今回の対象外。

| 分類 | ツール |
|---|---|
| サーバー | `get_server_info`（capabilities + diagnostics） |
| 録画権限 | `configure_recording_permission`（許可要求 / 設定を開く。録画しない） |
| 録画 | `start_recording` / `stop_recording` / `get_recording_status` / `import_recording` |
| 会議の閲覧 | `list_meetings`（`active_job` 付き）/ `search_transcripts` / `get_meeting` / `get_transcript` / `get_minutes` |
| 複数案の実行履歴 | `list_processing_runs` / `get_processing_run` / `get_processing_artifact` |
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

`regenerate.force=true` は `minutes_model`・`minutes_ensemble`・`transcription_model` がすべて null の従来処理だけで使う。
どれかのモデルを明示選択した会議では `invalid_argument` で拒否する。
同じ内容で明示的に新しい生成試行を始める場合は、実行と送信先をユーザーが確認した後、
`set_meeting_config` で `minutes_model.cache_epoch` を明示的に増やして保存する。
その保存結果を `expected_config` に渡し、新しい `request_id` と `force=false` で `regenerate` を呼ぶ。
送信や結果の成否が不明な試行は自動再送しない。同じ選択のまま通常の再生成を呼んでも、
結果不明の記録を迂回して再送することはできない。

明示的なモデル選択がある会議の `regenerate` と、`auto_regenerate=true` の `register_context` は、
送信確認時に取得した設定全体を `expected_config` として渡す。
サーバーは会議のロック内で現設定と照合し、確認後に変更されていれば `configuration_conflict` で拒否する。
旧来の生成と、生成しないコンテキスト登録では省略できる。指定した場合はどちらでも照合する。

### 複数案の生成と統合

`meeting_config.minutes_ensemble` は nullable な全置換の設定。
生成担当を2〜4件、統合担当を1件指定し、どちらも既存の `minutes_model_selection` を使う。

```json
{
  "generators": [
    {
      "id": "gen-11111111111111111111111111111111",
      "label": "第一案",
      "selection": {
        "provider": "openai-api",
        "connection_id": "conn-111122223333",
        "connection_revision": 1,
        "model_id": "fixture-openai-api-text-model",
        "parameters": {"max_tokens": 4096},
        "cache_epoch": 0
      }
    },
    {
      "id": "gen-22222222222222222222222222222222",
      "label": "第二案",
      "selection": {
        "provider": "anthropic-api",
        "connection_id": "conn-444455556666",
        "connection_revision": 1,
        "model_id": "fixture-anthropic-api-text-model",
        "parameters": {"max_tokens": 4096},
        "cache_epoch": 0
      }
    }
  ],
  "synthesizer": {
    "provider": "codex-app-server",
    "connection_id": "conn-fedcba987654",
    "connection_revision": 1,
    "model_id": "fixture-text-model",
    "parameters": {"reasoning_effort": "high"},
    "cache_epoch": 0
  }
}
```

IDは表示名・配列順と分離し、生成担当間で一意にする。
JSON Schemaは同一オブジェクトの重複を拒否し、型と実装は同じIDでlabelやselectionだけが異なる場合も拒否する。
labelは空白のみを拒否して最大80文字。generator ID、label、表示順はsemantic run/request identityから除外し、
変更だけなら同じrunと成果を採用してprovider送信を0件にする。
semantic identityはprovider・connection ID・model・実効content条件（connection revisionは除外）とcache_epochのmultiset、
同一scopeのdeterministic duplicate ordinal、synthesizerのcontent scopeで作る。現在のrevisionと認可は別に再検証する。
synthesizerへ渡すdraftは`(content_projection_sha256, deterministic_duplicate_ordinal)`順で固定し、
opaque generator ID、label、UI順序をpromptや並びの根拠にしない。
同じプロバイダ・接続の別モデルを使用でき、統合担当が生成担当と同じモデルでもよい。
同じ成果を共有した場合は、独立した二回の生成と表示しない。

省略は保持、nullは解除、objectは全置換。`minutes_model` との同時指定は更新後の全設定で拒否する。
モードの切替では以前の選択を明示的にnullにする。両方nullなら既存 `llm_provider` の経路を維持する。
会議・プロファイル・録画開始・取り込み・全設定CASと実行時の `expected_config` は同じ共通定義を使う。
保存だけでは生成しない。全担当・統合担当・ASRを一度のprovider transactionで検証し、各外部callと公開直前にも再検証する。
API担当を含む場合は `api_ok` を必要とし、許可を自動変更しない。

`capabilities.minutes_ensemble_limits` は上限通知だけで、実行可能性を保証しない。
`workflow.ensemble_generation` と各providerの能力・現在の認可を別に確認する。
通知する上限は `max_generators=4`、`max_concurrency=1`、`max_generation_attempts_per_run=64`、
`input_modalities=["text"]`、`max_reduction_depth=6`。
試行数は一runの再開・確認済み再送を累積し、成功再利用は新規試行に数えない。
この上限はSDK内部の全HTTP通信回数や会議全履歴の金額上限を示すものではない。

### 結果不明の議事録callを再試行する

`regenerate.minutes_retry` は `{run_id, node_id, call_id, blocked_attempt_id}` の閉じた確認オブジェクト。
run / node / call は現在確認している対象を指し、元の実行の出自はblocked attemptから復元して別表示する。
現在の全 `expected_config` と非nullの `minutes_ensemble` が必須。
`transcription_retry` との同時指定と `force=true` は拒否する。

現在の対象・設定・直近の不明receiptを送信前に照合し、新pendingの保存とともに一度だけ確認を消費する。
他の不明callや古いattemptの確認に流用しない。
epoch・新run・担当ID・接続IDの変更や、設定の解除・復元だけではunknownを解除しない。
同じrequest IDの復旧は同じ要求本文に限り、受付が不明なときは自動再送しない。
自動処理・コンテキスト登録にはminutes_retryを渡さない。

### 実行・成果物の読み取り

識別子は小文字hexの不透明IDで、`gen-`と`run-`は32桁、`slot-`、`node-`と`call-`は64桁、
`attempt-`と`artifact-`は32桁。内容のfingerprintとは別に扱う。
読み取りは会議IDとscopeを必ず照合し、任意のファイルパスを入力に取らない。

`list_processing_runs` はlimit既定20・1〜100、任意のURL-safe cursorは1〜256文字。
入力のcursorは省略できるがnullにはせず、応答のnext_cursorはnullableとする。
created_atの降順とrun_idで安定した順序を保ち、すべてのページで会議とscopeを検証する。
`get_processing_run` はrun_id、`get_processing_artifact` はrun_idとartifact_idを追加で受け取る。
成果物はそのrunに所属または再利用されたものに限る。単独生成・ASRに架空のrunを補わない。

一覧は軽量summaryだけを返し、詳細runは固定の設定・送信ポリシー、最大4096 nodes、最大8192 bindings、
公開版、部分成功、最大64試行、不明callを返す。run状態は
`prepared / running / blocked / succeeded / failed / cancelled / interrupted`。
有効な実行leaseがないrunningは、未解決pendingがなければinterrupted、あればblocked/node unknownに復元する。
揮発jobの消失から完了を推測せず、保存済みreceiptが有効なら再送せず復元する。

成果物kindと本文は閉じた対応にする。

| kind | payload | generation |
|---|---|---|
| `source_index` | `ensemble-source-index-v1`（packet ID 0〜64） | null |
| `source` | `ensemble-source-v1`（evidence 1〜32） | null |
| `draft_chunk` | `ensemble-document-v1` | 生成観測 |
| `draft` | `ensemble-draft-v1`（part 1〜64） | null |
| `synthesis` | `ensemble-document-v1` | 生成観測 |

evidenceは時刻、話者、Unicode codepointの半開文字区間、元segmentのhashを保持する。
claimは1〜8件のevidence参照を必要とし、action以外のowner/dueはnull、actionはnullまたは空白以外を含む120文字以下とする。
conflict questionは2〜4案、missing_contextは1〜4案を持ち、各案は1〜8件の参照を必要とする。
claim・question・alternativeのtextとactionのnonnull owner/dueは空白のみを拒否する。話者label/nameは原文の観測値としてnullまたは0〜512文字を保持し、空文字・空白とnullを区別する。
有限数・区間順・出現番号・参照解決・ID一意性は保存前に型とserverでも検査する。
`body_sha256` は公開payload全体の整合hashで、provider requestや下流projectionのhashとは別物。

artifact headerのrun_idは不変の元所有run。応答のrequested_run_idとbinding.run_idが現在の採用runを表す。
応答top-levelのreusedはrequiredでbinding.reusedと一致させ、UIがnested bindingを推測せず現在の再利用状態を読めるようにする。
reuse bindingは最大64件の公開dependency mappingと不透明なauthorization snapshot IDだけを返し、
秘密のsnapshot・prompt・raw request/response・credentialを返さない。
同じrequestでも応答projectionが変われば`content_projection_sha256`が変わり、古い下流成果を再利用しない。

直接生成した成果物だけが、要求selection、実効parameters、返却model、確認済みtoken usage、送信先、費用区分を持つ。
usageは6種類の任意counterを1件以上持ち、空のprovider usageはnullへ正規化する。欠損値を0と推測しない。
派生成果物のgenerationはnull。

runは2〜4件のcanonical slotを持ち、現在のgenerator IDと採用draftをsemantic slotへ対応付ける。
slotはselection scope hash、cache epoch、duplicate ordinalの順で固定し、ID・label・UI順序だけの変更は同じslotを採用する。
同じartifactを共有するslotはsynthesizerへ1件だけ渡し、別artifactでも同じprojectionならprojectionとwire duplicate ordinalで正規化する。
runのstatus=succeededは公開済みversionとsynthesis artifactを必須にし、blockedは空、run errorはnullにする。
status=blockedは1件以上のtyped blocked callを必須にする。nodeのsucceeded/reusedはartifactを必須にし、
succeededはreused=false、reused statusはreused=trueと一致させる。決定的な派生artifactのorigin=nullは正当なため禁止しない。

未解決の同一content fingerprintはblocked recordを1件だけ公開する。blocked_attempt_idはbarrierのcursor attempt、
targetはcursor attemptを所有するrun/node/call、latest_attempt_outcomeはcursorの保存状態と一致させる。
barrier key・cursor receipt・call・node・後続lineageの全attemptが同じfingerprintを持つことをtyped storage/serverで検査する。

結果不明callを確認付きで再試行した場合は、node・外部callのgeneration・公開bindingにrequired nullableなretry lineageを返す。
lineageは最初のunknown origin、最大64件のattempt/outcome鎖、最後の成功attemptを保持する。
別接続で成功しても元のunknownを上書きせず、成果物originは実際に成功した再送先を示す。
鎖のi>0は直前attemptの直接retryとし、privateなretry_of edgeは公開しない。Schemaは成功outcomeを最大1件、
resolved_by=nullなら0件、nonnullなら1件に制約する。未再試行はnull。先頭一致、attempt ID一意、private edge、
末尾成功attemptとresolved_byの同値はtyped storage/serverで検査する。生成artifactのlineageがnonnullならresolved_byもnonnullとし、
artifact originとの同値をtyped storage/serverで検査する。node・artifact generation・bindingのlineageも同値でなければならない。
この64件は同一content unknown barrierの生涯上限で、各runの64試行とは別に数える。
65件目は確認proofを消費せず、run予算を予約せず、送信0件で拒否し、元のunknown/chainを保持する。
安全なnode error reasonは`minutes_retry_limit_exceeded`。別contentの再試行枠は消費しない。

`get_minutes.provenance` はrequired nullable。旧版・単独版はnull、統合版だけはprovider=`ensemble`と
公開時のrun/input/generator/synthesizer artifact snapshotを双方向に一致させる。
実送信originは成果物から辿る。全status/kindのjobはtop-levelにrequired nullableな`processing_run_id`を返し、
統合runがpreparedとして永続化された後はrunning・failed・cancelledでもそのIDを維持する。
作成前、旧処理、export、provider setup等はnullにする。成功jobのresultにも同じrequired nullable fieldを返し、
top-levelとresultの両方がnonnullなら同じrun IDでなければならない。
`result.stages`に`minutes_ensemble`を含む成功regenerateはresultのrun IDをnonnullとし、legacy・単独生成はnullを許可する。

この契約ゲートの成功だけで、server・CLI・Swift を含む製品全体の実装完了とは扱わない。

### API 音声認識のモデル選択

`meeting_config.transcription_model` は独立した nullable な選択オブジェクト。
会議・プロファイル・新規録画・取り込み・読込応答で同じ型を使う。
更新時の省略は保持、null は解除。`transcription_engine` はローカル用の設定として残し、
API 選択中だけ使用しない。既存保存データに選択がなければ従来のローカル処理を維持する。

```json
{
  "provider": "openai-api",
  "connection_id": "conn-111122223333",
  "connection_revision": 1,
  "model_id": "whisper-1",
  "parameters": {},
  "cache_epoch": 0
}
```

`model_id` は `whisper-1` または `gpt-4o-transcribe-diarize` の明示 ID。
前者は word / segment、後者は diarized segment の時刻付き結果を使う。
時刻を扱えないモデルは候補表示しても選択できない。OpenAI 接続の roles は `llm` と `transcription` を含み、
`list_provider_models` の候補・ページ情報・UI キャッシュは role ごとに区別する。

接続の有効状態・版・認証・時刻対応・終了日と、`api_ok` を保存時と各送信直前に確認する。
共通 `language` は API 選択時だけ `auto` または小文字 2 文字の ISO 639-1 言語コードに制限する。
コードの実在性は実装側で検証し、`auto` は API の language を省略する。
`parameters` は省略時 `{}` で、初版は追加キーを受け付けない。
用語 prompt・話者名・参照音声・任意の応答形式・言語指定・Responses API のオプションを入れることはできない。
共通 `vocab_hints` は API 音声認識へ送らず、発話統合で引き続き使う。

保存・候補表示では音声を送らない。処理実行時に mic と system を別々に送り、API 課金の可能性を明示する。
送信は 16 kHz mono PCM16 WAV を 9,600,000 samples（10 分）以下の非重複区間に分け、mic → system の固定順で逐次実行する。
各ファイルはヘッダー込み 24,000,000 bytes 以下、全体は最大 144 区間・合計 24 時間の音声。
これらはアプリの送信上限であり、費用の上限や API 側の処理回数を保証しない。

### 結果不明の音声区間を再試行する

`cache_epoch` は省略時 0、非負整数。成功区間は epoch にかかわらず hash を照合して再利用する。
epoch だけの変更では完成した Transcript や下流の指紋を変えず、成功区間を再送しない。
送信後の切断・timeout・不正応答・保存失敗・取消は結果不明として台帳に残す。
再起動、DB 再構築、時間経過、force、選択の解除・復元でこの状態を消さない。
取消によって API 側の処理・課金が停止したとは保証しない。

結果不明の区間を一つ再送する場合は、ユーザーが対象と追加課金の可能性を確認した後、
選択の epoch を増やして保存し、`regenerate` に次の `transcription_retry` を渡す。
これは保存設定ではなく、一回の再試行確認である。

```json
{
  "input_fingerprint": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "chunk_fingerprint": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
  "blocked_epoch": 0
}
```

指紋は小文字 hex 64 文字の SHA256。`expected_config` とその非 null な `transcription_model` が必須で、
現在の `cache_epoch` が `blocked_epoch` より大きいこと、実入力・対象区間・直近の不明 epoch が一致することを送信前に確認する。
再送前の pending 保存で確認を一度消費し、再び結果不明になった場合は新たな確認を必要とする。
成功区間は再利用し、確認対象以外の不明区間を自動再送しない。epoch を増やすだけでは再送を許可しない。

`set_meeting_config` と `set_profile` の任意 `expected_config` は、保存前の全実効設定を照合する。
UI は通常の設定保存と epoch 更新でこの snapshot を渡し、同時変更を上書きしない。
不一致なら変更前に `configuration_conflict` を返す。省略した既存クライアントの保存動作は維持する。
初めて作るプロファイルは、`MeetingConfig` の既定値を比較基準としてから新しい設定を適用する。

`regenerate` と `auto_regenerate=true` の `register_context` は、API 音声認識でも設定全体の `expected_config` が必要。
録画・取り込みの自動処理は受理時の設定 snapshot を実行開始時に照合する。
自動処理とコンテキスト登録には不明区間の再送確認を渡さず、明示的な `regenerate` だけで確認する。

共通 `error.details.reason=transcription_outcome_unknown` は閉じた型として検証する。
必須項目は `stage=transcribe`、`reason`、`outcome_unknown=true`、二つの指紋、`blocked_epoch`、
`track`（mic / system）、全体計画の 0 始まり `chunk_index`、`chunk_count`、`completed_chunks`、
track 相対の半開区間 `start_sample / end_sample`、`sample_rate=16000`。
`provider / model_id / connection_id / connection_revision` は任意の安全な識別情報として返せる。
上流のエラー本文・音声・認証情報・パスは含めない。一般エラーの details は従来互換を保つ。
ジョブが失われても台帳を維持し、通常の再開では送信せず同じ確認情報を復元する。

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
v5 は音声認識の選択・対応 provider 一覧・OpenAI roles・型付き再試行情報を追加する。
旧クライアントの閉じた型と整合しないため major を上げるが、既存の選択省略・ローカル処理は維持する。
v6 はnullableなensemble設定・上限通知・確認付きcall再試行と読み取り3ツールを追加する。
新しい設定や再試行fieldを旧契約5以下の開発用接続へ送らない。

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
