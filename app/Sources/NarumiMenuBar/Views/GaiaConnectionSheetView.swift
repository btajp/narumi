import NarumiMenuBarCore
import SwiftUI

/// An optional server-wide connection, separate from meeting profiles. All settings access
/// stays behind NarumiClient; only the temporary SecureField input holds a credential here.
struct GaiaConnectionSheetView: View {
    let client: NarumiClient
    @Environment(\.dismiss) private var dismiss
    @State private var settings = GaiaConnectionSettings()
    @State private var confirmDisable = false

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            VStack(alignment: .leading, spacing: 6) {
                Text("Gaia 接続").font(.title3.bold())
                Text("会議ブリーフと Gaia へのエクスポートで使います。未設定でも録画・議事録生成を利用できます。")
                    .font(.callout)
                    .foregroundStyle(.secondary)
            }
            .padding(14)
            Divider()
            Form {
                currentConnectionSection
                connectionEditor
                connectionTestSection
            }
            .formStyle(.grouped)
            .disabled(!settings.canEdit)
            feedback
            Divider()
            footer
        }
        .frame(width: 640, height: 690)
        .interactiveDismissDisabled(settings.isBusy)
        .task { await load() }
        .onDisappear { settings.dismiss() }
        .confirmationDialog(
            "Gaia 接続を無効にしますか？", isPresented: $confirmDisable,
            titleVisibility: .visible
        ) {
            Button("無効にしてキーを削除", role: .destructive) {
                Task { await disable() }
            }
            Button("キャンセル", role: .cancel) {}
        } message: {
            Text("接続 URL と API キーを削除します。環境変数で設定されていても、Gaia は無効になります。録画・議事録は削除しません。")
        }
    }

    private var currentConnectionSection: some View {
        Section("現在有効な設定") {
            if let connection = settings.connection {
                LabeledContent("接続 URL", value: connection.url ?? "無効（接続しません）")
                    .textSelection(.enabled)
                LabeledContent("API キー", value: connection.hasAPIKey ? "設定済み（再表示不可）" : "未設定")
                LabeledContent("設定元", value: connection.source.label)
            } else if settings.needsEnvironmentRepair {
                Text("環境変数の接続設定が不正です。元の URL・キーは表示しません。")
                    .foregroundStyle(.orange)
            } else {
                Text("設定を読み込むと表示します。")
                    .foregroundStyle(.secondary)
            }
        }
    }

    private var connectionEditor: some View {
        Section {
            TextField("接続 URL", text: $settings.url, prompt: Text(GaiaConnectionSettings.defaultURL))
                .autocorrectionDisabled()
            Text("この Mac のループバック HTTP URL を指定します。ユーザー情報・クエリ・フラグメントは使えません。")
                .font(.caption)
                .foregroundStyle(.secondary)
            SecureField(
                "API キー", text: $settings.apiKey,
                prompt: Text(settings.needsEnvironmentRepair ? "新しい API キー（必須）" : "空欄なら変更しません"))
                .autocorrectionDisabled()
                .disabled(settings.clearAPIKey)
            if settings.needsEnvironmentRepair {
                Text("新しい接続 URL と、gaia-library で発行した agent ロールのキーを入力してください。元のキーは引き継ぎません。")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            } else {
                Text("gaia-library で発行した agent ロールのキーを使用してください。URL が同じなら、空欄で現在のキーを維持します。")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                Toggle("保存時に API キーを削除", isOn: Binding(
                    get: { settings.clearAPIKey },
                    set: { settings.setClearAPIKey($0) }))
                    .toggleStyle(.checkbox)
            }
            if settings.changesExistingURL {
                Text("URL を変更すると現在のキーは削除されます。新しいキーが必要な場合は、同時に入力してください。")
                    .font(.caption)
                    .foregroundStyle(.orange)
            }
            Text("保存した設定は環境変数より優先されます。API キーはサーバーに保存し、この画面では再取得・再表示しません。")
                .font(.caption)
                .foregroundStyle(.secondary)
        } header: {
            Text("接続設定の編集")
        }
    }

    private var connectionTestSection: some View {
        Section("接続テスト") {
            HStack {
                Button("接続テスト") {
                    Task { await test() }
                }
                .disabled(!settings.canTest)
                Text("現在有効な設定を使用・タイムアウト 5 秒")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            if settings.needsEnvironmentRepair {
                Text("新しい接続 URL と API キーを保存するとテストできます。")
                    .font(.caption)
                    .foregroundStyle(.orange)
            } else if settings.hasUnsavedChanges {
                Text("未保存の変更があります。保存してから接続テストを実行してください。")
                    .font(.caption)
                    .foregroundStyle(.orange)
            } else if settings.connection?.url == nil {
                Text("接続 URL を保存するとテストできます。")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            if let result = settings.currentTestResult {
                Label("接続を確認しました", systemImage: "checkmark.circle")
                    .foregroundStyle(.green)
                LabeledContent("サーバー", value: result.name)
                LabeledContent("サーバー版", value: result.version)
                LabeledContent("契約版", value: result.contractVersion)
                LabeledContent("クライアント", value: result.client.name)
                LabeledContent("ロール", value: result.client.role.rawValue)
                LabeledContent("既定 scope", value: result.client.defaultScope ?? "未設定（scope なし）")
                if result.client.role == .human {
                    Text("Narumi には agent ロールのキーを使用してください。承認は人間の操作で行います。")
                        .font(.caption)
                        .foregroundStyle(.orange)
                }
            }
        }
    }

    @ViewBuilder
    private var feedback: some View {
        if let operation = settings.operation {
            HStack {
                ProgressView().controlSize(.small)
                Text(operation.label).font(.callout)
            }
            .padding(12)
        } else if let error = settings.errorMessage {
            VStack(alignment: .leading, spacing: 6) {
                ScrollView {
                    Text(error)
                        .font(.callout)
                        .foregroundStyle(.red)
                        .textSelection(.enabled)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
                .frame(maxHeight: 90)
                if !settings.isLoaded {
                    Button("再読み込み") { Task { await load() } }
                }
            }
            .padding(12)
        } else if let notice = settings.notice {
            Text(notice)
                .font(.callout)
                .foregroundStyle(.secondary)
                .padding(12)
        }
    }

    private var footer: some View {
        HStack {
            Button("Gaia を無効にする…", role: .destructive) {
                confirmDisable = true
            }
            .disabled(!settings.canDisable)
            Spacer()
            Button("閉じる") {
                settings.dismiss()
                dismiss()
            }
            .keyboardShortcut(.cancelAction)
            .disabled(settings.isBusy)
            Button(settings.needsEnvironmentRepair ? "置換して保存" : "保存") { Task { await save() } }
                .keyboardShortcut(.defaultAction)
                .disabled(!settings.canSave)
        }
        .padding(12)
    }

    private func load() async {
        guard settings.beginLoad() else { return }
        do {
            let connection = try await client.gaiaConnection()
            settings.loaded(connection)
        } catch {
            failed(error)
        }
    }

    private func save() async {
        guard let request = settings.beginSave() else { return }
        do {
            let connection = try await client.setGaiaConnection(request)
            settings.saved(connection)
        } catch {
            failed(error)
        }
    }

    private func disable() async {
        guard let request = settings.beginDisable() else { return }
        do {
            let connection = try await client.setGaiaConnection(request)
            settings.saved(connection)
        } catch {
            failed(error)
        }
    }

    private func test() async {
        guard let request = settings.beginTest() else { return }
        do {
            let result = try await client.testGaiaConnection(request)
            settings.tested(result)
        } catch {
            failed(error)
        }
    }

    private func failed(_ error: Error) {
        let failure = error as? ToolFailure
        settings.failed(code: failure?.code ?? "error", message: failure?.message ?? error.localizedDescription)
    }
}
