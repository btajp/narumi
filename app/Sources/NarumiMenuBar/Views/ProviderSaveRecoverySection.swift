import NarumiMenuBarCore
import SwiftUI

struct ProviderSaveRecoverySection: View {
    @Bindable var store: ProviderSettingsStore
    @State private var retryAPIKey = ""
    @State private var confirmRetry = false
    @State private var confirmAdoption = false
    @State private var confirmDiscard = false
    @State private var adoption: ProviderConnection?

    var body: some View {
        if let summary = store.saveRecoverySummary {
            Section("保存結果の確認・復旧") {
                Text("前回の保存結果が不明なため、新しい保存要求を止めています。保存済み接続の確認、または前回と同じ要求の明示的な再試行で復旧できます。")
                    .font(.callout)
                Button("保存済み接続を再読み込み") { Task { await store.load(discardEdits: true) } }
                Text("再読み込みは設定の参照だけです。保存・認証情報の書き込みは行いません。")
                    .font(.caption).foregroundStyle(.secondary)
                if store.canAdoptSavedConnectionForRecovery, let connection = store.selectedConnection {
                    VStack(alignment: .leading, spacing: 5) {
                        Text("確認する保存済み接続").font(.subheadline.bold())
                        LabeledContent("名前", value: connection.displayName)
                        LabeledContent("接続 ID", value: connection.connectionID)
                        LabeledContent("現在の版", value: String(connection.revision))
                        LabeledContent("接続先", value: connection.endpoint ?? "未設定")
                        LabeledContent("認証情報", value: connection.credentialPresent ? "設定済み（再表示不可）" : "未設定")
                        Button("この接続を確認して再編集…") {
                            adoption = connection
                            confirmAdoption = true
                        }
                    }
                    Text("現在の設定を採用して再編集できますが、前回の保存・認証情報の更新の成功は確定しません。名前や接続先が一致しても自動で採用しません。左の一覧で別の候補を選べます。")
                        .font(.caption).foregroundStyle(.secondary)
                }
                if store.canDiscardMissingConnectionChange {
                    Button("削除済み接続への編集を破棄…", role: .destructive) { confirmDiscard = true }
                    Text("元の接続 ID が現在の一覧に存在しません。この操作は未確認の編集だけを破棄し、接続を新規作成しません。")
                        .font(.caption).foregroundStyle(.secondary)
                }
                if summary.receiptConfirmed {
                    Text("元の保存要求の受付結果は確認済みです。現在の設定を取得するため、一覧を再読み込みしてください。")
                        .font(.caption).foregroundStyle(.secondary)
                } else {
                    DisclosureGroup("前回と同じ要求を再確認・再試行") {
                        VStack(alignment: .leading, spacing: 7) {
                            LabeledContent("元の接続名", value: summary.displayName)
                            LabeledContent("プロバイダ", value: ProviderDisplay.name(summary.providerID))
                            LabeledContent("元の接続先", value: summary.endpoint)
                            LabeledContent("元の有効化", value: summary.enabled ? "有効" : "無効")
                            if summary.requiresAPIKeyReentry {
                                SecureField("前回と同じ API キー", text: $retryAPIKey, prompt: Text("再入力が必要です"))
                                    .autocorrectionDisabled()
                                Text("前回のキーは保持していません。同じキーを再入力してください。入力は送信前・失敗時・画面を閉じる際に消去します。")
                                    .font(.caption).foregroundStyle(.secondary)
                            } else {
                                Text(summary.providerID == .codexAppServer
                                    ? "API キー入力は不要です。前回と同じ接続設定だけを確認・保存し、ログイン操作は再送しません。"
                                    : "この要求の再確認に API キー入力は不要です。前回と同じキーの保持・削除指定を使います。")
                                    .font(.caption).foregroundStyle(.secondary)
                            }
                            Button("同じ保存を再確認・再試行…") { confirmRetry = true }
                                .disabled(!store.canRetryPendingSave || (summary.requiresAPIKeyReentry && retryAPIKey.isEmpty))
                            Text("前回と同じ要求 ID・入力で送信します。未配送なら保存し、完了済みなら結果だけを取得します。キー書き込み失敗などで再実行できない場合は、保存済み接続を確認して再編集してください。")
                                .font(.caption).foregroundStyle(.secondary)
                        }
                        .padding(.vertical, 5)
                    }
                }
            }
            .onDisappear { retryAPIKey = "" }
            .confirmationDialog("前回と同じ保存要求を送信しますか？", isPresented: $confirmRetry, titleVisibility: .visible) {
                Button("同じ要求で確認・再試行") {
                    let key = retryAPIKey.isEmpty ? nil : retryAPIKey
                    retryAPIKey = ""
                    Task { await store.retryPendingSave(apiKey: key) }
                }
                Button("キャンセル", role: .cancel) { retryAPIKey = "" }
            } message: {
                Text("未受付の場合だけ保存が行われます。新しい要求 ID は作りません。前回にキーを指定した場合は、同じキーの再入力が必要です。")
            }
            .confirmationDialog("現在の保存済み接続から編集を再開しますか？", isPresented: $confirmAdoption, titleVisibility: .visible) {
                Button("確認した接続から再編集") {
                    retryAPIKey = ""
                    if let adoption {
                        store.adoptSavedConnectionAfterReview(connectionID: adoption.connectionID, expectedRevision: adoption.revision)
                    }
                    adoption = nil
                }
                Button("キャンセル", role: .cancel) { adoption = nil }
            } message: {
                if let adoption {
                    Text("「\(adoption.displayName)」の現在の版 \(adoption.revision)・接続先・認証情報の有無を確認してください。未保存の入力を破棄してこの接続の編集に戻ります。前回の保存は再送せず、その成功も確定しません。")
                }
            }
            .confirmationDialog("存在しない接続への未確認の編集を破棄しますか？", isPresented: $confirmDiscard, titleVisibility: .visible) {
                Button("未確認の編集を破棄", role: .destructive) {
                    retryAPIKey = ""
                    store.discardMissingConnectionChangeAfterReview()
                }
                Button("キャンセル", role: .cancel) {}
            } message: {
                Text("元の接続 ID が一覧にないことを確認しました。保存要求を再送せず、未確認の編集状態を閉じます。")
            }
        }
    }
}
