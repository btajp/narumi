import NarumiMenuBarCore
import SwiftUI

struct ProviderConnectionStatusSection: View {
    @Bindable var store: ProviderSettingsStore
    @State private var confirmLogout = false

    var body: some View {
        Section("保存済み接続の確認") {
            if let connection = store.selectedConnection {
                LabeledContent("接続の有効化", value: connection.enabled ? "有効" : "無効")
                LabeledContent("API キー", value: connection.authMethod == .none
                    ? "不要" : (connection.credentialPresent ? "設定済み（再表示不可）" : "未設定"))
                LabeledContent("認証", value: ProviderDisplay.authentication(connection.authState))
                LabeledContent("モデル情報", value: ProviderDisplay.catalog(connection.catalogState))
                LabeledContent("議事録生成", value: ProviderDisplay.generation(connection.lastGenerationState))
                LabeledContent("最終確認", value: connection.checkedAt ?? "未確認")
                HStack {
                    Button("接続テスト") { Task { await store.testConnection() } }
                        .disabled(!store.canTest)
                    Button("認証確認を開始") { Task { await store.startAuthentication() } }
                        .disabled(!store.canTest)
                }
                Text("保存済みの認証情報とメタデータだけを確認します。会議データの送信・議事録生成は行いません。")
                    .font(.caption).foregroundStyle(.secondary)
                if let result = store.lastTest, !store.editor.hasUnsavedChanges {
                    Text(result.connected ? "接続テスト成功（議事録生成は未検証）" : "接続テストで接続を確認できませんでした")
                        .font(.callout).foregroundStyle(result.connected ? .green : .orange)
                    if let reason = ProviderDisplay.reason(result.reason) {
                        Text(reason).font(.caption).foregroundStyle(.secondary)
                    }
                }
                if let pending = store.pendingAuthentication {
                    HStack {
                        Text(ProviderDisplay.authOperation(pending.state)).font(.callout)
                        Spacer()
                        Button("認証操作の状態を確認") { Task { await store.checkAuthentication() } }
                        if pending.unresolved, pending.operationID != nil {
                            Button("認証操作を取消") { Task { await store.cancelAuthentication() } }
                        }
                    }
                    if pending.unresolved {
                        Text("応答が途切れても認証確認は再送しません。元の操作の状態を確認します。")
                            .font(.caption).foregroundStyle(.secondary)
                    }
                }
                if connection.authMethod == .apiKey {
                    Button("この接続からログアウト…", role: .destructive) { confirmLogout = true }
                        .disabled(!store.canUseSavedConnection || !connection.credentialPresent
                            || store.pendingAuthentication?.unresolved == true)
                    Text("ログアウトはこの接続のキーだけを削除します。ほかのアプリや接続の認証情報には触れません。")
                        .font(.caption).foregroundStyle(.secondary)
                }
            } else {
                Text("接続を保存すると、認証と接続先を確認できます。API キーは空欄でも保存できます。")
                    .foregroundStyle(.secondary)
            }
        }
        .confirmationDialog("この接続の認証情報を削除しますか？", isPresented: $confirmLogout, titleVisibility: .visible) {
            Button("ログアウト", role: .destructive) { Task { await store.logout() } }
            Button("キャンセル", role: .cancel) {}
        } message: {
            Text("再び利用するには API キーの入力が必要です。接続名と過去の議事録は保持します。")
        }
    }
}
