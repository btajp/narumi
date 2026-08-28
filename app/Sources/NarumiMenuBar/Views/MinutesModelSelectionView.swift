import NarumiMenuBarCore
import SwiftUI

struct MinutesModelSelectionView: View {
    @Binding var form: MinutesModelForm
    let catalog: MinutesModelCatalogStore
    let externalSendPolicy: String
    var isProfile = false
    @State private var showNewAttemptConfirmation = false
    @State private var preparedNewAttempt = false

    private var connection: ProviderConnection? { catalog.connection(form.connectionID) }
    private var response: ListProviderModelsResponse? { catalog.catalogs[form.connectionID] }
    private var selectableModels: [ProviderModelDescriptor] {
        response?.models.filter(MinutesModelForm.isTextMinutesModel) ?? []
    }
    private var selectedModel: ProviderModelDescriptor? {
        response?.models.first { $0.modelID == form.modelID }
    }
    private var revisionChanged: Bool {
        connection.map { $0.revision != form.connectionRevision } ?? false
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Picker("議事録の生成方法", selection: $form.mode) {
                ForEach(MinutesModelForm.Mode.allCases, id: \.self) { Text($0.title).tag($0) }
            }
            if form.mode == .codex {
                Text("OpenAI にテキストを送信し、ChatGPT の利用枠を使います。API 課金への切替は行いません。")
                    .font(.caption)
                connectionPicker
                modelPicker
                reasoningPicker
                if let message = form.validationMessage(
                    connections: catalog.connections, catalog: response, externalSendPolicy: externalSendPolicy) {
                    Text(message).font(.caption).foregroundStyle(.orange)
                }
                if let message = catalog.errorMessage {
                    Text(message).font(.caption).foregroundStyle(.red)
                }
                newAttemptControls
                Text(isProfile
                     ? "保存だけでは生成しません。新しい録画・取り込みにこの設定を適用し、自動処理が有効な場合は処理時にテキストを送信します。既存会議には適用しません。"
                     : "保存だけでは生成しません。保存後、議事録タブで送信内容を確認して再生成してください。")
                    .font(.caption).foregroundStyle(.secondary)
            } else {
                Text("議事録にも従来の LLM 設定を使います。保存すると Codex の議事録用選択を解除します。")
                    .font(.caption).foregroundStyle(.secondary)
            }
        }
        .task(id: form.catalogReadIdentity) {
            guard form.mode == .codex else { return }
            await catalog.loadCachedCatalog(connectionID: form.connectionID, selectedModelID: form.modelID)
        }
    }

    private var connectionPicker: some View {
        VStack(alignment: .leading, spacing: 6) {
            Picker("保存済み接続", selection: Binding(
                get: { form.connectionID },
                set: { form.selectConnection(catalog.connection($0)) })) {
                Text("接続を選択").tag("")
                ForEach(catalog.codexConnections, id: \.connectionID) {
                    Text("\($0.displayName)（rev \($0.revision)）").tag($0.connectionID)
                }
                if !form.connectionID.isEmpty, !catalog.codexConnections.contains(where: { $0.connectionID == form.connectionID }) {
                    Text("保存済み: \(form.connectionID)（利用不可）").tag(form.connectionID)
                }
            }
            if revisionChanged {
                Text("保存済み rev \(form.connectionRevision ?? 0) → 現在 rev \(connection?.revision ?? 0)")
                    .font(.caption).foregroundStyle(.orange)
                Button("変更後の接続を選び直す") { form.selectConnection(connection) }
                    .disabled(catalog.isLoading)
            }
            HStack {
                Button("接続一覧を再読込") {
                    Task { await catalog.loadCachedCatalog(connectionID: form.connectionID, selectedModelID: form.modelID) }
                }
                .disabled(catalog.isLoading)
                if catalog.isLoading { ProgressView().controlSize(.small) }
            }
            if catalog.codexConnections.isEmpty, !catalog.isLoading {
                Text("この画面を閉じて上部の「AI 接続」で Codex の接続を保存し、ログインを完了してください。")
                    .font(.caption).foregroundStyle(.secondary)
            }
        }
    }

    private var modelPicker: some View {
        VStack(alignment: .leading, spacing: 6) {
            Picker("議事録モデル", selection: Binding(
                get: { form.modelID },
                set: { id in form.selectModel(selectableModels.first { $0.modelID == id }) })) {
                Text("モデルを選択").tag("")
                ForEach(selectableModels, id: \.modelID) { model in
                    Text("\(model.displayName)（\(model.modelID)）").tag(model.modelID)
                }
                if !form.modelID.isEmpty, !selectableModels.contains(where: { $0.modelID == form.modelID }) {
                    Text("保存済み: \(form.modelID)（候補未確認）").tag(form.modelID)
                }
            }
            .disabled(catalog.isLoading || revisionChanged)
            HStack {
                Button("モデル候補を取得・更新") {
                    Task { await catalog.refreshModels(connectionID: form.connectionID) }
                }
                .disabled(catalog.isLoading || connection == nil || revisionChanged)
                if response?.nextCursor != nil {
                    Button("候補をさらに読み込む") {
                        Task { await catalog.loadMoreModels(connectionID: form.connectionID) }
                    }
                    .disabled(catalog.isLoading)
                }
            }
            Text("画面を開いたときは保存済み候補だけを読みます。「取得・更新」はモデル一覧を通信しますが、会議内容は送信しません。")
                .font(.caption).foregroundStyle(.secondary)
        }
    }

    private var reasoningPicker: some View {
        Picker("推論量", selection: $form.reasoningEffort) {
            Text("モデルの既定値").tag("")
            let options = MinutesModelForm.reasoningOptions(selectedModel)
            ForEach(options, id: \.self) { Text($0).tag($0) }
            if !form.reasoningEffort.isEmpty, !options.contains(form.reasoningEffort) {
                Text("\(form.reasoningEffort)（現在の候補にありません）").tag(form.reasoningEffort)
            }
        }
        .disabled(catalog.isLoading || selectedModel == nil || revisionChanged)
    }

    private var newAttemptControls: some View {
        DisclosureGroup("前回の結果が不明・新しい試行が必要なとき") {
            VStack(alignment: .leading, spacing: 6) {
                Text("同じ入力・選択では保存済みの結果を再利用します。結果不明の送信は自動再送しません。")
                Text("生成の試行番号: \(form.cacheEpoch)")
                Button("新しく生成を試す…") { showNewAttemptConfirmation = true }
                    .disabled(form.selection == nil || form.cacheEpoch == Int.max)
                    .confirmationDialog(
                        "新しい試行を準備しますか？", isPresented: $showNewAttemptConfirmation,
                        titleVisibility: .visible
                    ) {
                        Button("試行番号を増やす（保存後に再生成）") {
                            form.prepareNewAttempt()
                            preparedNewAttempt = true
                        }
                        Button("キャンセル", role: .cancel) {}
                    } message: {
                        Text("前の試行がサーバー側で完了している可能性があります。新しく試すと利用枠を重複して消費する場合があります。過去の議事録は保持します。この操作だけでは保存・送信しません。")
                    }
                if preparedNewAttempt {
                    Text("試行番号を変更しました。このフォームを保存し、議事録タブで再生成してください。")
                        .foregroundStyle(.orange)
                }
            }
            .font(.caption)
        }
    }
}
