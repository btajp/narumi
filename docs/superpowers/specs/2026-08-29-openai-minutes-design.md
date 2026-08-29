# 接続とモデルを指定した API・ローカル議事録生成

作成: 2026-08-29。対象: 0.4.0（実装・ソース検証済み）。
公開状況はソース検証と区別し、公開版を [GitHub Releases](https://github.com/btajp/narumi/releases) で確認する。

## 1. 公開範囲

0.3.0 の `minutes_model` を、Codex App Server / OpenAI API / Anthropic API /
ローカル Ollama に広げる。接続の保存からモデル選択、テキスト議事録生成までを
アプリで操作できることを完了条件にする。OpenAI 接続の追加だけでは公開しない。
Claude Agent SDK は既存の接続確認を維持し、生成の隔離を確認するまでは候補にしない。
音声認識、画像読解、発話統合、複数案の統合、全工程の設定移行は別の公開単位とする。

契約は 4.0.0 とし、既存 37 ツールを維持する。`minutes_model_providers` は
実装が対応する生成経路の一覧であり、認証済み・利用可能な接続の一覧ではない。
従来の `llm_provider` を画像読解・発話統合の設定として維持し、議事録の選択を共用しない。
配布アプリは同梱サーバーの契約 4.0.0 と厳密に照合し、製品 CLI はメジャー版 4 を要求する。
開発用の Swift 外部接続だけは、旧契約 2 / 3 を各版の対応範囲内で許可する。

## 2. アプリの操作

1. 「AI 接続」でプロバイダと接続名を選び、API ではキーを安全な入力欄から「接続を追加して保存」する。OpenAI / Anthropic の送信先は固定で変更不要。
2. 「実行環境」の「確認・準備」を完了する。OpenAI は「モデル一覧で接続を確認」、Anthropic / Ollama は「接続テスト」を使い、「接続先から候補を更新」でモデル一覧を取得する。会議本文は送らない。
3. 会議の「設定」または「プロファイル」で「議事録の生成方法」を「接続とモデルを指定」にする。「議事録プロバイダ」→「保存済み接続」→「議事録モデル」を選ぶ。画面表示時は保存済み候補だけを読み、「モデル候補を取得・更新」で明示的に照会する。
4. 対応する「推論量」と「出力上限（トークン）」を設定し、API では `api_ok` を明示する。空欄でよい項目は後述する。送信先・課金・テキスト送信を確認して保存する。
5. 議事録タブの「再生成」で保存済み設定と送信内容を確認し、「再生成を開始」を押す。保存や候補取得だけでは生成しない。

キー未設定、未認証、無効、接続版の不一致、未準備、未検証モデルは理由付きで選択不可。
OpenAI の接続確認成功は、キーでモデル一覧を取得できたことを示す。
残高、生成権限、実生成の成功を確認したとは表示しない。
API キーは既存の Keychain 経路を使い、環境変数・別接続へフォールバックしない。
Codex は [専用ログインの設計](2026-08-29-codex-minutes-design.md) を維持する。
API キーは不要で、固定版 CLI 0.150.1 を準備し、確認コードを公式デバイス画面へ
利用者が入力して承認する。既存 Codex の認証情報や他アプリの callback ポートを使わない。
コードと URL は一時表示だけとし、結果不明でも別認証方式へ自動変更しない。

## 3. モデルとパラメータ

`provider` / `connection_id` / `connection_revision` / `model_id` / `parameters` /
`cache_epoch` の既存形を維持する。設定更新で `minutes_model` を省略すれば保持、
null は解除、オブジェクトは全体置換である。UI の「従来設定」は解除に対応する。

| 経路 | パラメータ | 送信許可 |
|---|---|---|
| Codex App Server | `reasoning_effort` | `subscription_ok` または `api_ok` |
| OpenAI API | 対応する `reasoning_effort`、`max_tokens` | `api_ok` |
| Anthropic API | `max_tokens` | `api_ok` |
| ローカル Ollama | `max_tokens` | `local_only` 以上、ローカルモデル確認必須 |

`max_tokens` は OpenAI の `max_output_tokens`、Anthropic の `max_tokens`、
Ollama の `options.num_predict` へ変換する。
narumi の要求上限は 1〜32,768。既知のモデル出力上限を超える要求は拒否する。
省略時は 4,096 と既知のモデル上限の小さい方、不明ならアプリ上限 4,096 を使用する。
これはアプリの処理制限であり、不明な `max_output_tokens` を既知の能力として埋めない。
Codex ではこのパラメータを許可せず、0.3.0 の出力制限に関する説明を維持する。

OpenAI は `/v1/models` と出典付きの固定能力表の共通部分だけを選択可能にする。
初期の固定表は次の 6 alias と 3 snapshot。アカウントの一覧にない ID は選択可能にしない。

| alias | 確認済み snapshot | 推論量の候補・既定値 |
|---|---|---|
| `gpt-5.6-sol` | なし | `none / low / medium / high / xhigh / max`、既定 `medium` |
| `gpt-5.6-terra` | なし | 同上 |
| `gpt-5.6-luna` | なし | 同上 |
| `gpt-5.4` | `gpt-5.4-2026-03-05` | `none / low / medium / high / xhigh`、既定 `none` |
| `gpt-4.1` | `gpt-4.1-2025-04-14` | 指定不可。`reasoning` 自体を送らない |
| `gpt-4.1-mini` | `gpt-4.1-mini-2025-04-14` | 同上 |

対応する推論モデルで「推論量」を空欄にした場合は、固定表の既定値を使う。
GPT-5.6 では `mode=standard` / `context=current_turn` も明示する。
未知 ID・未確認 snapshot・fine-tuned ID を接頭辞から類推しない。
不明なモデルは一覧に残し、能力未確認と表示する。context や出力上限の不明値は null を維持し、
アプリ側の制限値をモデルの能力として保存しない。モデルや推論設定を別候補へ自動変更しない。

Models API の `shutdown_date` は `availability_expires_on`（`YYYY-MM-DD | null`）として保持する。
画面は日付を「提供終了予定日」として表示し、UTC 日付が当日以降なら選択・生成を拒否する。
キャッシュ済み候補でも呼出時に日付を再判定する。これは narumi の保守的な規則であり、
公式の正確な終了時刻やタイムゾーンを確認・保証したという意味ではない。

Ollama は数値 loopback の接続先と `:local` 選択を維持し、送信前に実モデルの
digest を再検査する。変更を検出した場合は送信せず、選び直しを要求する。
Modelfile に組み込まれた system / messages が継承される場合があり、それらの隔離や
narumi のプロンプトだけで生成されることは保証しない。

## 4. 外部通信と結果の扱い

OpenAI は `https://api.openai.com/v1/responses` へ直接 REST で送る。
新しい SDK・グローバル CLI は導入しない。キー、モデル、出力上限を明示し、
`tools=[]`、`tool_choice=none`、`store=false`、`background=false`、`stream=false`、
`truncation=disabled` を固定する。会話 ID、過去の応答 ID、外部プロンプトは送らない。
既存の固定 Markdown プロンプトを使い、分割要約と議事録をテキストで作成する。
各応答の commentary は議事録本文に含めず、最終回答を採用する。
推論設定は対応を確認したモデルにだけ送る。OpenAI の出力上限には推論トークンも含まれる。

OpenAI / Anthropic / Ollama の内蔵 HTTP 経路は proxy・redirect・環境由来のヘッダーや
TLS 鍵ログを使わず、自動再送しない。
絶対期限・応答サイズ上限・取消を適用する。API キー本体と Bearer ヘッダー全体の
反射を拒否し、上流のエラー本文・例外をログや公開応答へ保存しない。
出力の model / status / message / refusal を確認し、未完了・拒否・不正な応答を成功保存しない。

同期応答の取消は接続を切る操作であり、サービス側の処理や課金の取消を保証しない。
送信後の timeout・切断・不正応答・成果保存失敗は結果不明として記録し、自動で再送しない。
`store=false` は OpenAI の保持が一切ないという意味ではない。サービス側の
不正利用監視等の保持条件と区別する。

Codex は固定 SDK の既存境界を維持する。通常の要求・ストリーム再試行を無効にしても、
HTTP リダイレクト、401 後の認証回復、未処理が確定した HTTP/2 NACK の再送は残る。
全通信が同じ URL に一度だけ送られる保証ではなく、narumi が結果不明の生成を
自動で開始し直さないことと区別する。

## 5. 保存・再生成・競合

0.3.0 の設定照合、provider lease、成功 checkpoint、取消、結果不明時の保護を一般化する。
接続・認証・モデル能力・実行環境・送信許可を各呼出の直前に再確認する。
`expected_config` は選択した 4 系統に共通とし、設定の競合時は送信より前に停止する。
選択した 4 系統とも `force=true` を拒否し、新規試行は重複利用の可能性を表示して
`cache_epoch` で明示する。UI は「前回の結果が不明・新しい試行が必要なとき」→
「新しく生成を試す…」→「試行番号を増やす（保存後に再生成）」の順に進める。
この操作だけでは保存・送信せず、フォーム保存後に確認して再生成する。
CLI / MCP は保存後の `expected_config`、新しい `request_id`、`force=false` を使う。

モデルだけの変更では文字起こし・発話統合をやり直さない。
指紋には接続版、モデル、実効パラメータ、能力表・アダプタ・runtime の版を含める。
日時だけの候補更新を内容変更と誤認しない。過去の議事録は不変の版として維持する。
API の利用量やその一部が応答にない場合は不明として扱い、ゼロや無料と推測しない。
呼出上限と出力上限は設けるが、厳密な金額上限を保証するものではない。

## 6. 検証と参照

0.4.0 のソース検証結果（2026-08-29）:

- 契約 4.0.0 の先行ゲートは 657 件成功。
- Python 全 3,844 件成功、5 件 skip、2 件対象外（532.57 秒）。Swift 全 565 件成功。
- Python 2 パッケージの wheel、Swift build、Ruff check / format（299 ファイル）、0.4.0 の版整合も成功。
- 保存・再読込・選択どおりの生成、禁止送信、キー反射、接続やモデルの変更、取消、送信後の切断、成功部分の再利用を fake で検証した。
- 固定版の実 Codex CLI 0.150.1 でも、loopback 以外の通信を禁止した合成 fixture で指定モデル・`tools=[]` を確認した。成功応答とヘッダー段階・ストリーム途中の切断は各ケースで生成 POST が 1 回。切断時は結果不明とし、自動再送しなかった。固定 SDK の通信上の制約は引き続き別に扱う。

実 API キー・会議データは使っていない。実 ChatGPT ログイン・実生成、実 API・
実 Keychain・更新後 GUI は未検証。署名・公証・公開物照合・公開の成否は、
上記のソース検証結果と分けて確認・記録する。

一次情報（2026-08-29確認）:
[Models](https://developers.openai.com/api/reference/resources/models)、
[Responses](https://developers.openai.com/api/reference/cli/resources/responses/methods/create)、
[推論](https://developers.openai.com/api/docs/guides/reasoning)、
[データ保持](https://developers.openai.com/api/docs/guides/your-data)、
[提供終了](https://developers.openai.com/api/docs/deprecations)。
モデル能力は各モデルの公式ページを固定能力表から参照する。
