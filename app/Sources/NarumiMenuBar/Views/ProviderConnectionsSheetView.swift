import NarumiMenuBarCore
import SwiftUI

struct ProviderConnectionsSheetView: View {
    private enum Navigation { case connection(String), newConnection, reload }

    @Environment(\.dismiss) private var dismiss
    let store: ProviderSettingsStore
    @State private var confirmDelete = false
    @State private var confirmDiscard = false
    @State private var pendingNavigation: Navigation?

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            VStack(alignment: .leading, spacing: 6) {
                Text("AI プロバイダの接続").font(.title3.bold())
                Text("接続・認証・実行環境とモデル候補を確認します。議事録のモデルは会議の「設定」またはプロファイルの「議事録の生成方法」で選択して保存します。")
                    .font(.callout).foregroundStyle(.secondary)
            }
            .padding(14)
            Divider()
            HStack(spacing: 0) {
                connectionList
                Divider()
                Form {
                    ProviderSaveRecoverySection(store: store)
                    ProviderConnectionEditor(store: store)
                    ProviderConnectionStatusSection(store: store)
                    ProviderRuntimeSection(store: store)
                    ProviderModelCatalogSection(store: store)
                }
                .formStyle(.grouped)
                .disabled(!store.canEdit)
            }
            feedback
            Divider()
            footer
        }
        .frame(width: 980, height: 760)
        .interactiveDismissDisabled(store.isBusy)
        .task { await store.load() }
        .task(id: store.isLoaded ? store.selectedConnectionID : nil) { await store.loadModels() }
        .task(id: store.needsPolling) {
            while store.needsPolling && !Task.isCancelled {
                do { try await Task.sleep(for: .seconds(2)) } catch { return }
                await store.refreshOperations()
            }
        }
        .onDisappear { store.dismiss() }
        .confirmationDialog("接続と認証情報を削除しますか？", isPresented: $confirmDelete, titleVisibility: .visible) {
            Button("接続を削除", role: .destructive) { Task { await store.deleteConnection() } }
            Button("キャンセル", role: .cancel) {}
        } message: {
            Text("この接続専用の認証情報も削除します。過去の議事録は保持します。使用中または会議・プロファイルから参照中の接続は削除できません。")
        }
        .confirmationDialog("未保存の編集を破棄しますか？", isPresented: $confirmDiscard, titleVisibility: .visible) {
            Button("編集を破棄して続ける", role: .destructive) {
                if let pendingNavigation { navigate(pendingNavigation, confirmed: true) }
                pendingNavigation = nil
            }
            Button("キャンセル", role: .cancel) { pendingNavigation = nil }
        } message: {
            Text("未保存の名前・接続先・API キー入力を破棄します。保存済みの接続設定は変更しません。")
        }
    }

    private var connectionList: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Text("接続一覧").font(.headline)
                Spacer()
                Button { navigate(.newConnection) } label: { Image(systemName: "plus") }
                    .help("接続を追加")
                    .accessibilityLabel("接続を追加")
                    .disabled(!store.canAddConnection)
            }
            ScrollView {
                VStack(spacing: 6) {
                    ForEach(store.connections, id: \.connectionID) { connection in
                        Button { navigate(.connection(connection.connectionID)) } label: {
                            VStack(alignment: .leading, spacing: 4) {
                                Text(connection.displayName).font(.subheadline.bold())
                                Text(ProviderDisplay.name(connection.providerID)).font(.caption)
                                Text(connection.enabled ? ProviderDisplay.authentication(connection.authState) : "接続は無効")
                                    .font(.caption).foregroundStyle(.secondary)
                            }
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .padding(10)
                            .background(
                                store.selectedConnectionID == connection.connectionID ? Color.accentColor.opacity(0.12) : .clear,
                                in: RoundedRectangle(cornerRadius: 8))
                        }
                        .buttonStyle(.plain)
                        .disabled(!store.canEdit)
                    }
                    if store.isLoaded && store.connections.isEmpty {
                        Text("接続はまだありません。右の項目を入力して保存してください。")
                            .font(.callout).foregroundStyle(.secondary)
                    }
                }
            }
            Text("Codex App Server は ChatGPT ログイン、OpenAI API は API キーで利用します。")
                .font(.caption).foregroundStyle(.secondary)
            Button("一覧を再読み込み") { navigate(.reload) }
                .disabled(store.isBusy)
        }
        .padding(12)
        .frame(width: 230)
    }

    @ViewBuilder private var feedback: some View {
        if let operation = store.operation {
            HStack {
                ProgressView().controlSize(.small)
                Text(operation.label).font(.callout)
            }.padding(12)
        } else if let error = store.errorMessage {
            Text(error).font(.callout).foregroundStyle(.red).padding(12)
        } else if let notice = store.notice {
            Text(notice).font(.callout).foregroundStyle(.secondary).padding(12)
        }
    }

    private var footer: some View {
        HStack {
            Button("接続を削除…", role: .destructive) { confirmDelete = true }
                .disabled(!store.canDelete)
            Spacer()
            Button("閉じる") {
                store.dismiss()
                dismiss()
            }
            .keyboardShortcut(.cancelAction)
            .disabled(store.isBusy)
            Button(store.editor.isCreating ? "接続を追加して保存" : "接続設定を保存") {
                Task { await store.save() }
            }
            .keyboardShortcut(.defaultAction)
            .disabled(!store.canSave)
        }
        .padding(12)
    }

    private func navigate(_ navigation: Navigation, confirmed: Bool = false) {
        if case .connection(let id) = navigation, id == store.selectedConnectionID { return }
        if store.editor.hasUnsavedChanges && !confirmed {
            pendingNavigation = navigation
            confirmDiscard = true
            return
        }
        switch navigation {
        case .connection(let id): store.selectConnection(id)
        case .newConnection: store.newConnection()
        case .reload: Task { await store.load(discardEdits: true) }
        }
    }
}
