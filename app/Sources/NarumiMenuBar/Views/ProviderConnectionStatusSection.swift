import AppKit
import NarumiMenuBarCore
import SwiftUI

struct ProviderConnectionStatusSection: View {
    @Environment(\.openURL) private var openURL
    @Bindable var store: ProviderSettingsStore
    @State private var confirmLogout = false

    var body: some View {
        Section("保存済み接続の確認") {
            if let connection = store.selectedConnection {
                LabeledContent("接続の有効化", value: connection.enabled ? "有効" : "無効")
                if connection.authMethod == .chatgpt {
                    LabeledContent("ChatGPT ログイン", value: connection.credentialPresent
                        ? "専用のログイン情報あり（再表示不可）" : "未ログイン")
                } else {
                    LabeledContent("API キー", value: connection.authMethod == .none
                        ? "不要" : (connection.credentialPresent ? "設定済み（再表示不可）" : "未設定"))
                }
                LabeledContent("認証", value: ProviderDisplay.authentication(connection.authState))
                LabeledContent("モデル情報", value: ProviderDisplay.catalog(connection.catalogState))
                LabeledContent("議事録生成", value: ProviderDisplay.generation(connection.lastGenerationState))
                LabeledContent("最終確認", value: connection.checkedAt ?? "未確認")
                HStack {
                    Button(connection.authMethod == .chatgpt ? "ChatGPT でログイン" : "認証確認を開始") {
                        Task { await store.startAuthentication() }
                    }
                    .disabled(!store.canAuthenticate)
                    Button(connection.providerID == .openaiAPI ? "モデル一覧で接続を確認" : "接続テスト") {
                        Task { await store.testConnection() }
                    }
                        .disabled(!store.canTest)
                }
                Text(ProviderDisplay.connectionVerificationScope(connection.providerID))
                    .font(.caption).foregroundStyle(.secondary)
                if connection.authMethod == .chatgpt {
                    Text(store.selectedProvider?.runtime.state == .ready
                        ? "ログイン後、「接続先から候補を更新」でモデル一覧を取得してください。"
                        : "ログインを始めるには、下の「実行環境」で「確認・準備」を完了してください。")
                        .font(.caption).foregroundStyle(.secondary)
                } else if connection.providerID == .openaiAPI, store.selectedProvider?.runtime.state != .ready {
                    Text("接続確認を始めるには、下の「実行環境」で内蔵アダプタの「確認・準備」を完了してください。")
                        .font(.caption).foregroundStyle(.secondary)
                }
                if let result = store.lastTest, !store.editor.hasUnsavedChanges {
                    Text(ProviderDisplay.connectionTestResult(result))
                        .font(.callout).foregroundStyle(result.connected ? .green : .orange)
                    if let reason = ProviderDisplay.reason(result.reason) {
                        Text(reason).font(.caption).foregroundStyle(.secondary)
                    }
                }
                if let pending = store.pendingAuthentication {
                    if let authorization = store.deviceAuthorization {
                        LabeledContent("確認コード") {
                            Text(authorization.userCode.displayValue).font(.body.monospaced())
                        }
                        HStack {
                            Button("確認コードをコピー") {
                                store.copyAuthorizationUserCode { code in
                                    NSPasteboard.general.clearContents()
                                    return NSPasteboard.general.setString(code, forType: .string)
                                }
                            }
                            Button("ブラウザで続ける") {
                                if let url = store.browserAuthorizationURL { openURL(url) }
                            }
                        }
                        Text("公式の OpenAI デバイスログイン画面に確認コードを入力して承認します。完了すると、この画面の認証状態が更新されます。")
                            .font(.caption).foregroundStyle(.secondary)
                    }
                    HStack {
                        Text(ProviderDisplay.authOperation(pending.state)).font(.callout)
                        Spacer()
                        Button("認証操作の状態を確認") { Task { await store.checkAuthentication() } }
                        if pending.unresolved, pending.operationID != nil {
                            Button("認証操作を取消") { Task { await store.cancelAuthentication() } }
                        }
                    }
                    if let reason = pending.reasonMessage {
                        Text(reason).font(.caption).foregroundStyle(.secondary)
                    }
                    if pending.unresolved {
                        Text("応答が途切れても認証確認は再送しません。元の操作の状態を確認します。")
                            .font(.caption).foregroundStyle(.secondary)
                    }
                }
                if connection.authMethod != .none {
                    Button("この接続からログアウト…", role: .destructive) { confirmLogout = true }
                        .disabled(!store.canLogout)
                    Text("ログアウトはこの接続の認証情報だけを削除します。ほかのアプリや接続の認証情報には触れません。")
                        .font(.caption).foregroundStyle(.secondary)
                }
            } else {
                Text(store.editor.providerID == .codexAppServer
                    ? "接続を保存すると、専用の ChatGPT ログインを開始できます。API キーは不要です。"
                    : "接続を保存すると、認証と接続先を確認できます。API キーは空欄でも保存できます。")
                    .foregroundStyle(.secondary)
            }
        }
        .confirmationDialog("この接続の認証情報を削除しますか？", isPresented: $confirmLogout, titleVisibility: .visible) {
            Button("ログアウト", role: .destructive) { Task { await store.logout() } }
            Button("キャンセル", role: .cancel) {}
        } message: {
            Text(store.selectedConnection?.authMethod == .chatgpt
                ? "再び利用するには、この接続で ChatGPT にログインしてください。既存の Codex のログイン、接続名、過去の議事録は変更しません。"
                : "再び利用するには API キーの入力が必要です。接続名と過去の議事録は保持します。")
        }
    }
}
