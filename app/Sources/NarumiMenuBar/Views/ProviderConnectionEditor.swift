import NarumiMenuBarCore
import SwiftUI

struct ProviderConnectionEditor: View {
    @Bindable var store: ProviderSettingsStore

    var body: some View {
        Section(store.editor.isCreating ? "接続の追加" : "接続設定") {
            if store.editor.isCreating {
                Picker("プロバイダ", selection: Binding(
                    get: { store.editor.providerID }, set: { store.selectProvider($0) }
                )) {
                    ForEach(store.availableProviderIDs, id: \.self) { providerID in
                        Text(ProviderDisplay.name(providerID)).tag(providerID)
                    }
                }
                if store.availableProviderIDs != ProviderID.connectionPickerOrder {
                    Text("このサーバーが公開していないプロバイダは選択できません。6 種すべてを利用するには、アプリと同梱サーバーを更新してください。")
                        .font(.caption).foregroundStyle(.orange)
                }
            } else {
                LabeledContent("プロバイダ", value: ProviderDisplay.name(store.editor.providerID))
                Text(store.editor.providerID == .openAICompatibleAPI
                     ? "プロバイダを変える場合は接続を追加してください。認証方式と API 形式はこの接続で変更できます。"
                     : "プロバイダを変える場合は接続を追加してください。保存済み接続の認証方式は変更しません。")
                    .font(.caption).foregroundStyle(.secondary)
            }
            TextField("接続名", text: $store.editor.displayName, prompt: Text("用途が分かる名前"))
            Text("複数の接続を区別する表示名です。例: 議事録用 Codex。会議名やモデル ID は変更しません。")
                .font(.caption).foregroundStyle(.secondary)
            Toggle("この接続を有効にする", isOn: $store.editor.enabled)
                .toggleStyle(.checkbox)
            Text("無効にしても設定と認証情報は保持します。削除する場合は下の「接続を削除」を使います。")
                .font(.caption).foregroundStyle(.secondary)
            if store.editor.providerID == .codexAppServer {
                LabeledContent("送信先", value: ProviderConnectionSettings.codexEndpoint)
                LabeledContent("認証方式", value: "ChatGPT ログイン（API キー不要）")
                Text("接続名を入力して保存し、下の「実行環境」で準備後、「ChatGPT でログイン」から確認コードを取得します。公式画面でコードを入力して承認します。API キーの入力は不要です。")
                    .font(.caption).foregroundStyle(.secondary)
                Text("デバイスコード認証を利用できない場合はログインを停止します。ほかのログイン方式へ自動では切り替えません。")
                    .font(.caption).foregroundStyle(.secondary)
                Text("ログイン情報はこの接続専用の環境に保存します。既存の Codex アプリや CLI のログイン・設定は共有しません。")
                    .font(.caption).foregroundStyle(.secondary)
                Text("生成には ChatGPT の利用枠を使います。利用条件や上限は契約プランに従い、無制限・無料とは扱いません。接続の保存やログインだけでは会議データを送信しません。")
                    .font(.caption).foregroundStyle(.secondary)
            } else if store.editor.providerID == .ollama {
                TextField("接続先", text: $store.editor.endpoint, prompt: Text(ProviderConnectionSettings.ollamaEndpoint))
                    .autocorrectionDisabled()
                Text("この Mac の Ollama に数値 IP の HTTP/HTTPS URL で接続します。既定は http://127.0.0.1:11434 です。DNS 名・外部 IP・認証情報・クエリは指定できません。HTTPS は証明書の検証も必要です。")
                    .font(.caption).foregroundStyle(.secondary)
                if !store.editor.isEndpointValid {
                    Text("接続先には 127.x.x.x または [::1] の HTTP/HTTPS URL を指定してください。localhost は使えません。")
                        .font(.caption).foregroundStyle(.orange)
                }
                LabeledContent("認証方式", value: "認証なし（ローカル接続）")
                Text("API キーは不要です。ローカル接続でも、モデルがローカル実行できるかは別に確認します。")
                    .font(.caption).foregroundStyle(.secondary)
            } else if store.editor.providerID == .openAICompatibleAPI {
                TextField("API ベース接続先", text: $store.editor.endpoint,
                          prompt: Text("https://api.example.com/v1"))
                    .autocorrectionDisabled()
                Text("モデル一覧は接続先の /models、生成は選択した API 形式の /responses または /chat/completions を使います。末尾の / は付けず、必要な /v1 などのパスを含めてください。")
                    .font(.caption).foregroundStyle(.secondary)
                if !store.editor.isEndpointValid {
                    Text("外部接続は HTTPS と API キーが必須です。認証なしまたは HTTP は 127.x.x.x / [::1] の数値ループバックだけ指定できます。ユーザー情報・クエリ・フラグメント・相対パスは使えません。")
                        .font(.caption).foregroundStyle(.orange)
                }
                if store.editor.requiresAPIKeyReentryForEndpointChange {
                    Text("接続先を変更する場合は、この接続先用の API キーを再入力するか、保存済みキーの削除を明示してください。以前の接続先のキーは自動で再利用しません。")
                        .font(.caption).foregroundStyle(.orange)
                }
                Picker("認証方式", selection: Binding(
                    get: { store.editor.authMethod }, set: { store.editor.selectAuthMethod($0) }
                )) {
                    Text("API キー").tag(ProviderAuthMethod.apiKey)
                    Text("認証なし（ループバック専用）").tag(ProviderAuthMethod.none)
                }
                Text("外部ホストへの認証なし接続は保存できません。認証方式を変えると、未保存の API キー入力を消去します。")
                    .font(.caption).foregroundStyle(.secondary)
                if store.editor.usesAPIKey {
                    apiKeyFields
                } else {
                    Text("API キーは送信しません。ローカルの互換サーバーが外部へ中継する可能性があるため、議事録生成には api_ok が必要です。")
                        .font(.caption).foregroundStyle(.secondary)
                }
                Picker("API 形式", selection: $store.editor.apiSurface) {
                    Text("Responses API (/responses)").tag(ProviderAPISurface.responses)
                    Text("Chat Completions API (/chat/completions)").tag(ProviderAPISurface.chatCompletions)
                }
                Text("接続先が実装する形式を明示します。失敗時に別形式へ自動で切り替えません。")
                    .font(.caption).foregroundStyle(.secondary)
                if store.editor.apiSurface == .chatCompletions {
                    Picker("出力上限フィールド", selection: $store.editor.chatMaxTokensField) {
                        Text("max_tokens").tag(ProviderChatMaxTokensField.maxTokens)
                        Text("max_completion_tokens").tag(ProviderChatMaxTokensField.maxCompletionTokens)
                    }
                    Text("Chat Completions の要求で使うフィールドを固定します。失敗時にもう一方へ自動で切り替えません。")
                        .font(.caption).foregroundStyle(.secondary)
                }
                Text("接続先は任意の互換サービスです。ローカル URL でも外部へ中継する可能性があり、無料・ローカル完結とは扱いません。")
                    .font(.caption).foregroundStyle(.secondary)
            } else {
                LabeledContent("送信先", value: store.editor.normalizedEndpoint)
                LabeledContent("認証方式", value: "API キー（API 課金）")
                apiKeyFields
                if store.editor.providerID == .openaiAPI {
                    Text("OpenAI API のキーを入力してください。送信先は固定で変更不要です。ChatGPT のログインや利用枠とは別に API 利用料が発生します。保存だけでは接続確認や議事録生成は行いません。")
                        .font(.caption).foregroundStyle(.secondary)
                }
                if store.editor.providerID == .claudeAgentSDK {
                    Text("Claude のサブスクリプションログインは使いません。Anthropic API キーによる API 課金です。生成可否は保存後の実行環境・モデル状態に表示され、未確認のモデルは明示的な検証が必要です。")
                        .font(.caption).foregroundStyle(.secondary)
                }
            }
            if store.saveNeedsReconciliation {
                Text("保存結果を確認できていません。重複する接続を作成しないよう、保存・追加を停止しています。「一覧を再読み込み」で保存済み接続を確認してください。")
                    .font(.caption).foregroundStyle(.orange)
            } else if store.revisionConflict {
                Text("別の操作で設定が変更されています。「一覧を再読み込み」で最新の設定を確認してください。")
                    .font(.caption).foregroundStyle(.orange)
            } else if store.editor.hasUnsavedChanges {
                Text("未保存の変更があります。接続テスト・認証確認の前に保存してください。")
                    .font(.caption).foregroundStyle(.secondary)
            }
        }
    }

    @ViewBuilder private var apiKeyFields: some View {
        SecureField("API キー", text: $store.editor.apiKey, prompt: Text("空欄なら現在のキーを保持"))
            .autocorrectionDisabled()
            .disabled(store.editor.clearAPIKey)
        Text("キーは Keychain に保存し、読み戻し・再表示しません。新規接続で空欄の場合は未設定として保存します。保存の成功・失敗・画面を閉じる際に入力を消去します。")
            .font(.caption).foregroundStyle(.secondary)
        if !store.editor.isCreating {
            Toggle("保存時に API キーを削除", isOn: Binding(
                get: { store.editor.clearAPIKey }, set: { store.editor.setClearAPIKey($0) }
            ))
            .toggleStyle(.checkbox)
        }
    }
}
