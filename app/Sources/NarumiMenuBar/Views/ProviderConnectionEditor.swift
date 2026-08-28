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
                    ForEach(store.providers, id: \.providerID) { provider in
                        Text(provider.displayName).tag(provider.providerID)
                    }
                }
            } else {
                LabeledContent("プロバイダ", value: ProviderDisplay.name(store.editor.providerID))
                Text("プロバイダを変える場合は接続を追加してください。保存済み接続の認証方式は変更しません。")
                    .font(.caption).foregroundStyle(.secondary)
            }
            TextField("接続名", text: $store.editor.displayName, prompt: Text("用途が分かる名前"))
            Text("複数の接続を区別する表示名です。例: 議事録用 Anthropic。会議名やモデル ID は変更しません。")
                .font(.caption).foregroundStyle(.secondary)
            Toggle("この接続を有効にする", isOn: $store.editor.enabled)
                .toggleStyle(.checkbox)
            Text("無効にしても設定と API キーは保持します。削除する場合は下の「接続を削除」を使います。")
                .font(.caption).foregroundStyle(.secondary)
            if store.editor.providerID == .ollama {
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
            } else {
                LabeledContent("送信先", value: ProviderConnectionSettings.anthropicEndpoint)
                LabeledContent("認証方式", value: "API キー（API 課金）")
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
                if store.editor.providerID == .claudeAgentSDK {
                    Text("Claude のサブスクリプションログインは未対応です。SDK の認証・履歴の隔離が確認できるまで、モデル候補が表示されても生成可能とは扱いません。")
                        .font(.caption).foregroundStyle(.orange)
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
}
