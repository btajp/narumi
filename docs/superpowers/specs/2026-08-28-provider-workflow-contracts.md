# プロバイダ設定・生成方式の公開契約案

作成: 2026-08-28。最終確認: 2026-08-29。全体の契約設計。
PR 1 の接続関連 9 ツールは `contracts/` に追加し、実装・fake 統合検証済み。
工程別設定・実行計画・複数生成などの後続ツールは未登録。実際の正本は `contracts/`。
上位設計は [画面・設定設計](2026-08-28-provider-workflow-design.md)、
実行時の意味は [実行・保存設計](2026-08-28-provider-workflow-execution.md)を参照。

## 1. 版と互換性

PR 1 の契約は **2.0.0** とし、接続管理に対応した範囲で公開する。後続の契約変更は semver に従って版を上げる。
工程別設定への移行、実行前確認、設定版による更新制御は、既存の書込要求の意味を変える。
既存スキーマには `additionalProperties: false` もあるため、単なる項目追加として互換を主張しない。
製品は PR 1 で **0.2.0** に更新済み。後続 PR も確認可能な機能単位で新しい製品版を公開する。

アプリ・CLI はサーバーの契約 major と対応機能を検査する。
v1 サーバーには新しい引数を送らず、更新が必要と表示する。
v2 サーバーへ v1 のモデル未指定要求を送っても、暗黙に外部モデルへ補完しない。
既存の会議・議事録の読取は残し、書込時の移行は差分確認を経る。
旧クライアントによる不完全な更新を拒否するため、新設定の書込には期待する設定版を必須にする。

## 2. 共通モデル

### ProviderDescriptor / ProviderConnection

| 型・項目 | 内容 |
|---|---|
| `provider_id` | レジストリ名。UI の表示名と分離する |
| `roles` | `transcription / diarization / llm` の対応範囲 |
| `auth_methods` | アダプタで利用可能な認証方式。未承認の方式を含めない |
| `runtime` | 準備状態、実行版、承認済み配布カタログ、互換性エラー。ユーザーの絶対パスをログへ転載しない |
| `runtime.active_setup / last_setup` | 準備 job ID・開始要求 ID・リソース ID・状態。該当なしは null。画面の再表示でも準備を追跡できる |
| `connection_id / revision` | 接続の安定 ID と変更検出用の版 |
| `display_name / provider_id` | 接続の表示名と種別。作成後の種別変更は不可 |
| `enabled` | 接続を有効にするか。無効化しても資格情報・設定・過去の成果は削除しない |
| `endpoint / auth_method` | 非秘密の送信先・認証方式。公式 API は固定、Ollama は loopback 限定 |
| `credential_present` | 秘密の有無だけ。秘密自体やその値を推測できる表示は返さない |
| `auth_state / catalog_state` | 未確認・必要・成功・失敗を分離し、確認日時を付ける |
| `active_auth` | 操作 ID・開始要求 ID・サーバーインスタンス ID・状態。進行中でなければ null |
| `last_generation_state` | 実際の生成の最終結果。接続テスト結果と混ぜない |

保存済み接続に秘密参照を持つ場合でも、公開モデルへ Keychain の内部識別子を出さない。
接続変更は `expected_revision` を照合し、不一致は `configuration_conflict` とする。
稼働中の接続の送信先・認証方式・runtime を置き換えない。無効化は次の呼出を止める。
削除は非稼働時だけとし、使用中プロファイルの参照が残る場合は変更を求める。
過去の成果の非秘密メタデータは、接続削除後も維持する。

### ModelDescriptor

| 項目 | 内容 |
|---|---|
| `model_id / display_name` | API に送る ID と表示名 |
| `resolved_revision` | 取得できる固定版・ローカル digest。取得不能なら null |
| `input_modalities / output_modalities` | 文章・画像・音声。未知は対応扱いにしない |
| `roles / timestamp_support` | 工程への適合性。時刻は `none / segment / word / diarized_segment` |
| `context_window / max_output_tokens` | 確認できた上限。未取得なら null と理由 |
| `parameter_schema` | 当該モデルで有効な型・enum・範囲。任意の CLI 引数や HTTP ヘッダーは不可 |
| `availability / reason` | 利用可、未準備、未確認、認証必要、非対応、廃止など |
| `source / fetched_at` | 取得元・日時。実行内容のキャッシュキーには日時を入れない |
| `billing` | 認証方式による費用区分、確認済み単価、取得日時。不明は null |

モデルが機能を持っていても、SDK・アダプタ・送信許可が対応しなければ利用不可とする。
モデル一覧 API で返る ID 全件を、すべての工程へそのまま表示・有効化しない。

### ModelSelection

`provider / connection_id / model_id / parameters / cache_epoch` を持つ。
`connection_id` は外部 API・SDK・Ollama では必須、同梱のローカルエンジンでは null とする。
`model_id` は空欄不可。自動推奨は UI 上で選択を助けるだけで、保存時には明示 ID にする。
`parameters` は既定 `{}`。保存時・解決時に `parameter_schema` で検査し、未知キーを拒否する。
`cache_epoch` は既定 0 の非負整数。alias 実体の明示更新時にだけ進める。

モデル ID の変更時にはパラメータを再検証し、未対応値を黙って捨てない。
API キー、token、URL、コマンド、環境変数、ファイルパスを `parameters` で注入できないようにする。
ローカルのモデル配置は runtime の専用設定で管理し、会議設定に任意の読取パスを持たせない。

### ProcessingConfig

| 項目 | 型・意味 |
|---|---|
| `transcription.selection` | 必須の `ModelSelection`。初期の `timestamp_mode` は `native` のみ |
| `diarization` | `ModelSelection` または null（使わない） |
| `visual_speakers` | 同上。画像対応を必須にする |
| `transcript_integration` | 同上。発話を区間単位で統合する担当 |
| `minutes.mode` | `plain / single / ensemble` |
| `minutes.generators` | `{id, label, selection}` の配列。ID は安定・一意、label だけの変更は再生成不要 |
| `minutes.synthesizer` | 議事録案の統合担当。`ModelSelection` または null |
| `limits` | 並列数、API 呼出数、入力音声秒数、任意の金額上限、金額不明時の扱い |
| `send_grants` | 工程・接続・非秘密の送信先識別子・認証の費用区分・データ種別に対する明示許可 |

`plain` は生成担当 0 件・統合担当 null、`single` は 1 件・null、
`ensemble` は 2〜4 件・統合担当必須。並列数は 1〜2 の範囲で既定 2 とする。
生成を使わないことと、モデル選択が途中であることを区別する。
編集中の未完成フォームはアプリの一時状態とし、曖昧なモデル選択を有効な設定として保存しない。
録画のみの場合は、明示的なローカル・要約なし構成で保存できる。

`limits.max_api_calls` と `max_audio_seconds` は正の整数で明示する。
呼出数は処理 run が発行する外部生成・音声認識要求の累計であり、明示再試行も数える。
接続のメタデータ取得・認証・結果照会はこの生成要求数と分けて記録する。
SDK 内部の要求数を把握できない場合は別の不明情報を付け、HTTP 要求数の保証と混同しない。
音声秒数は会議の長さ（トラックの終了位置の最大値）であり、マイクとシステム音声を二重加算しない。
課金見積りの音声量は別に、実際に送る各トラック・chunk・再試行を合算する。
`max_usd` は非負 decimal または null、通貨は USD に固定し換算額を上限と混ぜない。
`unknown_cost` は `deny / acknowledge`。`acknowledge` は厳密な金額上限の保証ではない。
有限の金額上限が指定され、呼出上限額を計算できない経路は実行不可。
出力 token の上限は各モデルの `parameters` に解決して重複する正本を作らない。
呼出数・金額の上限は会議の台帳に対する累計上限とし、run・モデル・request ID の変更でリセットしない。
既支出、結果不明の予約、実行中の予約を含めた残枠をプレビューに表示する。
上限の変更を保存しても処理は始めず、追加予算の実行確認で台帳版も照合する。

`MeetingConfig` の言語・本人名・語彙・`external_send_policy` は維持し、
三つの旧エンジン選択フィールドを `processing: ProcessingConfig` に置き換える。
`external_send_policy` と `send_grants` は両方満たす必要がある。
送信許可の設定は、接続・認証方式・データ種別との整合をサーバーが検査する。

### ResolvedSelection / ProcessingPlan / RecordingAuthorizationEnvelope

`ResolvedSelection` は要求された選択に加え、実効モデル・設定、能力、送信先、
費用区分、アダプタ版、runtime 版、モデル実体が未確認かどうかを持つ。
API キー・token・秘密の hash は含めない。

`ProcessingPlan` は次を持つ。

- `plan_fingerprint`、既知入力の版・hash、設定版、接続版、許可の版、予算台帳版。
- 依存順の `nodes`。各 node に ID、担当、入力参照、`run / reuse / conditional / blocked`、理由。
- `expansion_rules`。未生成の入力に依存する分割・段階的要約の固定手順と最大呼出数。
- `transfers`。工程、実際の送信先、音声・文字起こし・ブリーフ・画像・他担当の案の区分。
- `cost_estimate`。確認済み単価と利用量に基づく範囲、または不明とその理由。
- `limits / warnings / can_execute`。確認前に処理を始めない。

計画の指紋は実行許可そのものではない。サーバーは実行直前に入力・設定・許可を照合する。
時刻や一時的な表示状態ではなく、処理・送信に影響する値を比較する。
ASR の結果など未生成の入力は依存 node への参照で表し、空の hash や仮の本文で確定扱いにしない。
計画は既知入力と処理手順を固定し、実行中に確定した入力 hash・展開済み node は別の実行記録へ追記する。
その展開が確認済みのモデル・送信先・データ種別・上限を超える場合は、次の送信前に停止して再確認する。

録画前のプレビューは `RecordingAuthorizationEnvelope` を返す。
これはプロファイルの不変スナップショット、選択と実効能力、接続版、許可、上限、
処理手順の版を束ねた事前許可であり、まだない音声の hash や具体的な node 数は含めない。
`authorization_fingerprint` を録画開始時に照合し、停止後は具体的な計画がその範囲に収まるかを検査する。
録画前と停止後の `plan_fingerprint` が同一であることは要求しない。

## 3. 新規ツール

書込は `request_id`、会議を読む操作は既存の scope 検査を使う。
新しい認証操作・外部生成は常駐サーバーを必要とし、CLI の in-process へ暗黙に切り替えない。
下表は契約の候補であり、実装・UI・テストが揃うまで manifest に登録しない。

| ツール | 主な入力 | 主な結果・動作 |
|---|---|---|
| `list_providers` | なし | アダプタ・役割・認証方式・runtime 状態。外部通信なし |
| `list_provider_connections` | なし | 非秘密の接続一覧・版・状態 |
| `set_provider_connection` | ID、省略時は新規、種別、表示名、enabled、認証方式、必要な endpoint、write-only のキー、期待版 | 設定保存。生成・ログインを暗黙に行わない |
| `delete_provider_connection` | ID、期待版、`confirm: true` | 非稼働・参照条件を検査し、資格情報を削除 |
| `prepare_provider_runtime` | 種別、配布カタログのリソース ID・期待版、準備 / 更新 | 同梱 runtime・ローカルモデルの取得。`provider_setup` job を返す |
| `authenticate_provider_connection` | ID、期待版、`action: start / cancel / logout`、必要なら操作 ID | 公式認証を開始・取消・解除。認証操作 ID と公開状態だけを返す |
| `get_provider_auth_status` | 接続 ID、操作 ID または `start_request_id` | 開始応答を失っても状態照会できる。token を返さない |
| `test_provider_connection` | 接続 ID、期待版 | 認証・メタデータ疎通。会議送信・生成なし |
| `list_provider_models` | 接続 ID またはローカル種別、役割、cursor、refresh | モデル候補・能力・取得日時。外部取得は明示更新時 |
| `preview_processing` | 対象、必要なら設定案、scope、実行意図 | 副作用のない実行計画・差分。LLM・音声認識は呼ばない |
| `list_processing_runs` | 会議 ID、scope、cursor | 過去 run の状態・作成順・成果概要。再起動後も履歴を列挙できる |
| `get_processing_run` | 会議 ID、run ID、scope | 永続化済み run・node・成果一覧・結果不明の呼出 |
| `get_processing_artifact` | 会議 ID、成果 ID、scope | 登録済みの案・根拠・メタデータ。任意パス読取ではない |
| `reconcile_processing_call` | 会議 ID、run ID、call ID、scope | 正式に対応する結果照会のみ。生成の再送はしない。非対応は理由を返し不明状態を維持 |

`set_provider_connection` のキー省略は保持、null は削除。新規の API 接続でキーがない場合は
未設定状態として保存し、生成利用は不可。接続種別の変更は新規作成で行う。
認証方式・送信先変更時は旧資格情報を自動で流用しない。
初期実装は接続ごとに資格情報・認証セッション・SDK 状態を分離し、接続間で秘密参照を共有しない。
ログアウトは指定接続だけに適用する。他接続への影響を伴う操作は提供せず、共有・一括解除は別途設計する。
同じ外部アカウントの利用枠が共有されることは、ローカル認証状態の分離とは区別して表示する。
`enabled` の変更も接続版を更新する。再有効化で認証・モデル・既存の送信許可を確認済みに戻さない。

runtime 準備は署名・hash・版・ライセンスを検査できる配布カタログのリソースだけを扱う。
任意 URL・コマンド・インストール先は受け付けず、許可されたモデル取得先への通信を画面に表示する。
`get_job_status / cancel_job` で進捗・取消を扱い、稼働中の runtime は置き換えない。
同じ provider の準備は一つずつ受け付け、進行中・直近の準備 job を `list_providers` から返す。
応答喪失時も開始要求 ID から同じ受付を再取得できるようにし、再ダウンロードを勝手に開始しない。

認証 URL は公式の応答であること、許可したスキーム・host・callback 先であることを検査する。
ブラウザを開く便宜はアプリが担えるが、認証の状態変更は公開ツール経由とする。
操作は接続版と `server_instance_id` に結び付け、別サーバーの idle 応答で不明状態を解除しない。
長時間待つ認証を一つの MCP 応答でブロックせず、開始後は操作 ID で状態照会する。
開始要求 ID と操作 ID の対応は秘密なしで永続化する。応答喪失時は同じ開始要求 ID で照会し、
再起動で継続を確認できなければ不明を返す。新しいログイン開始を自動で繰り返さない。

`preview_processing.target` は会議、録画前のプロファイル、取り込み元ファイルの三種類。
取り込み元は既存の絶対パス・通常ファイル検証を再利用する。
録画前は未確定の音声長や入力 hash を確定済みとせず、自動処理許可の上限を確認する用途に限定する。
実行意図は通常処理、失敗 run の再開、指定 node の明示再実行を区別する。

## 4. 既存ツールの変更案

| ツール | 変更 |
|---|---|
| `get_server_info` | 契約 major と利用可能な workflow 機能を返す。アダプタ名の存在だけで完成を示さない |
| `get_profile / list_profiles` | 設定と `revision`、既定情報を返す |
| `set_profile` | 期待版を必須にし、処理設定・送信許可・上限を保存。生成はしない |
| `get_meeting` | 実効選択、設定版、適用元と不変の `profile_snapshot`、最新 run の要約を返す |
| `set_meeting_config` | 期待版と `edit / apply_profile / restore_snapshot` を受ける。未指定の独立項目は維持 |
| `start_recording` | プロファイル版と録画のみ / 自動処理の選択を保存。自動処理は事前許可の指紋を照合 |
| `stop_recording` | 必ず録画確定を先行。自動処理の許可・上限を満たさなければ確認待ちを返す |
| `import_recording` | 処理する場合は入力 hash を含む計画を照合。取り込みだけも可能 |
| `regenerate` | `expected_plan_fingerprint`、再開対象、必要なら再実行対象を受け、run / job ID を返す |
| `get_job_status / cancel_job` | run と工程を結び付ける。取消は外部課金停止の保証ではない |
| `get_minutes` | 完成版の生成担当・実効モデル・統合担当・根拠・利用量への参照を追加 |
| `export_minutes` | 選択した完成版と送信許可を検査。未完成の案を自動公開しない |

`processing` の更新は全体の原子的置換とし、未知キー・不整合を拒否する。
個々の工程を UI で編集しても、保存は読取時の版に対する完全な `ProcessingConfig` を送る。
言語・本人名・語彙・scope など従来の独立した項目は、未指定ならそのまま維持する。
`edit` は完全な `processing` を受ける。`apply_profile` はプロファイル ID と期待版を受け、
サーバーがその版の処理設定・送信許可・上限と適用元スナップショットを原子的に保存する。
`restore_snapshot` は当該会議に保存したスナップショット ID を受け、同じ範囲を復元する。
どちらも scope・エクスポート先・言語・本人名・語彙などを暗黙に変更せず、処理設定の適用操作として表示する。
旧会議に元のスナップショットがなければ復元不可とし、現在のプロファイルから推測で復元しない。

v2 の再実行では、曖昧な全体 `force` を使わず、計画中の node ID を指定する。
音声認識をやり直す場合は音声の再送・再課金を計画に表示する。
結果不明の外部呼出を再試行する要求には、その call ID に対する重複課金の確認を必要とする。
同じ `request_id` の再送は同じ受付結果を返し、新しい run を作らない。
成果の根拠参照は不変の source 成果 ID とその中の発話・区間 ID を含める。
過去版を取得するときに、同じ名前の最新 transcript や統合済み発話へ参照を置き換えない。

## 5. 秘密・検証・エラー

### 常駐サーバーの認証

v2 は数値 loopback への TLS 接続とクライアント認証を必須にする。
正規の起動経路が生成する所有者限定 bootstrap ファイルから、証明書 pin・port・instance ID を読む。
親ディレクトリは 0700、ファイルは 0600 とし、所有者・通常ファイル・symlink でないことを検査する。
pin は未認証 HTTP や接続先の自己申告から取得・更新しない。
TLS と pin の検証後にだけ、Keychain のインスタンス用 client token を送る。
別 OS ユーザーや偽サーバーからの接続を拒否し、同一 OS ユーザー権限の侵害・root は保証範囲外とする。

Host / Origin の検査も維持し、Web ページからの意図しない要求を拒否する。
ローカル接続では環境プロキシ・リダイレクトを使わず、DNS を経由しない。
外部の公式 API への HTTPS は通常の DNS・hostname 証明書検証を行い、数値 IP に置き換えない。
他の MCP クライアントは同じ認証か、所有ユーザーの stdio ブリッジを使う。UI 専用の権限は作らない。
旧 HTTP は認証不要の最小の互換性案内だけとし、会議読取・設定変更・生成・秘密の受付は許可しない。
認証失敗時に旧 HTTP や in-process の経路へ降格しない。現在の v1 の通信方式は今回変更しない。

### 秘密入力と記録

秘密を扱うツールは write-only 保護を拡張し、認証済みの常駐接続でのみ利用可能にする。
生成 CLI は write-only 項目を通常の文字列オプションにせず、非表示プロンプトか専用の stdin 入力にする。
秘密を含む文字列の `--json` は拒否し、機械利用では stdin からの JSON 入力を用意する。
ネイティブの Keychain ヘルパーにも匿名 pipe で渡し、argv・環境変数・一時ファイルへ入れない。
キーを応答、ログ、バリデーションエラー、監査、要求キャッシュへ平文保存しない。
要求再送の照合は秘密を再表示しない方式で実装し、成功・失敗の両方を検証する。

既存の `policy_violation / engine_unavailable / busy / scope_denied` に加え、v2 で
`configuration_conflict / plan_stale / authentication_required / model_unavailable /
budget_exceeded / external_result_unknown` を定義する。
外部の例外本文を直接返さず、理由・対象 node・回復操作だけを安全な公開モデルに変換する。
モデル不明、画像非対応、時刻なし、認証不足、上限超過を「内部エラー」にまとめない。

## 6. 契約先行の検証

実装 PR ごとに必要な契約を先に追加し、`uv run pytest pipeline/tests/contracts` を通す。
その後にサーバー・自動生成 CLI・Swift 型・画面・対応テストを実装する。
生成 pydantic 型は従来どおりコミットしない。

必須例: ローカル単独、API 単独、Codex と Claude の複数案、別モデルでの統合、
無効なモデル、非対応推論設定、禁止送信、秘密の省略と削除、古い設定版、古い計画、
時刻なしモデル、部分失敗、結果不明、取消、旧契約との非互換検出。
応答を失った認証開始の照会、無効化・再有効化、履歴の列挙、元プロファイル復元、
偽サーバー・別 OS ユーザーの拒否、秘密の argv 非露出、条件付き計画、録画前の事前許可、
再試行でも減らない既支出と不明予約も検証する。
`contracts/`・Python・Swift・CLI のフィールドと例を同じテストデータで照合する。
