import NarumiMenuBarCore
import SwiftUI

struct TranscriptionModelSelectionView: View {
    @Binding var form: TranscriptionModelForm
    let catalog: TranscriptionModelCatalogStore
    let externalSendPolicy: String
    let language: String
    var isProfile = false

    private var connection: ProviderConnection? { catalog.connection(form.connectionID) }
    private var connections: [ProviderConnection] { catalog.connections(for: form.provider) }
    private var response: ListProviderModelsResponse? { catalog.catalogs[form.connectionID] }
    private var models: [ProviderModelDescriptor] { response?.models ?? [] }
    private var selectedModel: ProviderModelDescriptor? {
        guard response?.connectionRevision == form.connectionRevision else { return nil }
        return models.first { $0.modelID == form.modelID }
    }
    private var revisionChanged: Bool { connection.map { $0.revision != form.connectionRevision } ?? false }
    private var canUseConnection: Bool {
        guard let connection, !revisionChanged else { return false }
        return catalog.connectionUnavailableReason(connection) == nil
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Picker("文字起こしの処理方法", selection: $form.mode) {
                Text(TranscriptionModelForm.Mode.local.title).tag(TranscriptionModelForm.Mode.local)
                Text(TranscriptionModelForm.Mode.selected.title).tag(TranscriptionModelForm.Mode.selected)
                    .disabled(!catalog.supportedProviders.contains(form.provider))
            }
            if form.mode == .selected {
                Text("送信先: OpenAI（https://api.openai.com/v1/audio/transcriptions）。音声の外部送信と従量 API 課金を許可する api_ok の明示が必要です。ChatGPT の利用枠とは別です。")
                    .font(.caption)
                connectionPicker
                modelPicker
                modelDetails
                LabeledContent("音声認識で使う言語", value: language == "auto" ? "auto（API 側で判定）" : language)
                Text("言語は共通の「言語」欄で設定します。ja・en など小文字2文字の ISO 639-1 コード、または auto を指定してください。auto では API の言語指定を省略します。")
                    .font(.caption).foregroundStyle(.secondary)
                Text("登録語彙は API 音声認識には送らず、発話統合で引き続き使います。話者名の指定・参照音声・追加プロンプトなどの設定項目はありません。")
                    .font(.caption).foregroundStyle(.secondary)
                if let message = form.validationMessage(
                    connections: catalog.connections, catalog: response, externalSendPolicy: externalSendPolicy,
                    language: language, supportedProviders: catalog.supportedProviders, providers: catalog.providers) {
                    Text(message).font(.caption).foregroundStyle(.orange)
                }
                if let message = catalog.errorMessage {
                    Text(message).font(.caption).foregroundStyle(.red)
                }
                audioDisclosure
                Text(isProfile
                     ? "保存や候補表示だけでは音声を送りません。この設定は新しい録画・取り込みに適用し、自動処理が有効な場合は処理時に音声を送信します。既存会議の設定は変更しません。"
                     : "保存や候補表示だけでは音声を送りません。保存後、処理内容を確認して再生成してください。")
                    .font(.caption).foregroundStyle(.secondary)
            } else {
                Text("保存済みのローカル文字起こしエンジンを使います。この状態で保存すると API の選択を解除し、ローカルエンジンの設定を維持します。")
                    .font(.caption).foregroundStyle(.secondary)
            }
        }
        .task(id: form.catalogReadIdentity + "/" + catalog.supportedProviders.joined(separator: ",")) {
            guard form.mode == .selected else { return }
            await catalog.loadCachedCatalog(connectionID: form.connectionID, selectedModelID: form.modelID)
        }
    }

    private var connectionPicker: some View {
        VStack(alignment: .leading, spacing: 6) {
            Picker("音声認識用の OpenAI 接続", selection: Binding(
                get: { form.connectionID }, set: { id in selectConnection(id) })) {
                Text("接続を選択").tag("")
                ForEach(connections, id: \.connectionID) { candidate in
                    let reason = catalog.connectionUnavailableReason(candidate)
                    Text("\(candidate.displayName)（rev \(candidate.revision)）" + (reason.map { " — \($0)" } ?? ""))
                        .tag(candidate.connectionID).disabled(reason != nil)
                }
                if !form.connectionID.isEmpty, !connections.contains(where: { $0.connectionID == form.connectionID }) {
                    Text("保存済み: \(form.connectionID)（利用不可）").tag(form.connectionID).disabled(true)
                }
            }
            .disabled(catalog.isLoading || !catalog.supportedProviders.contains(form.provider))
            if let connection, let reason = catalog.connectionUnavailableReason(connection) {
                Text(reason).font(.caption).foregroundStyle(.orange)
            }
            if revisionChanged {
                Text("保存済み rev \(form.connectionRevision ?? 0) → 現在 rev \(connection?.revision ?? 0)")
                    .font(.caption).foregroundStyle(.orange)
                Button("変更後の接続を選び直す") { selectConnection(form.connectionID) }
                    .disabled(catalog.isLoading || connection.map { catalog.connectionUnavailableReason($0) != nil } != false)
            }
            HStack {
                Button("接続一覧を再読込") {
                    Task { await catalog.loadCachedCatalog(connectionID: form.connectionID, selectedModelID: form.modelID) }
                }
                .disabled(catalog.isLoading)
                if catalog.isLoading { ProgressView().controlSize(.small) }
            }
            if connections.isEmpty, !catalog.isLoading {
                Text("「AI 接続」で OpenAI API の接続とキーを保存し、実行環境の準備・接続確認を完了してください。Codex のログイン情報は使いません。")
                    .font(.caption).foregroundStyle(.secondary)
            }
        }
    }

    private var modelPicker: some View {
        VStack(alignment: .leading, spacing: 6) {
            Picker("音声認識モデル", selection: Binding(
                get: { form.modelID }, set: { id in selectModel(id) })) {
                Text("モデルを選択").tag("")
                ForEach(models, id: \.modelID) { model in
                    let reason = TranscriptionModelForm.modelUnavailableReason(model)
                    Text("\(model.displayName)（\(model.modelID)）" + (reason.map { " — \($0)" } ?? ""))
                        .tag(model.modelID).disabled(reason != nil)
                }
                if !form.modelID.isEmpty, !models.contains(where: { $0.modelID == form.modelID }) {
                    Text("保存済み: \(form.modelID)（候補未確認）").tag(form.modelID).disabled(true)
                }
            }
            .disabled(catalog.isLoading || !canUseConnection)
            HStack {
                Button("音声認識の候補を取得・更新") {
                    Task { await catalog.refreshModels(connectionID: form.connectionID) }
                }
                .disabled(catalog.isLoading || !canUseConnection)
                if response?.nextCursor != nil {
                    Button("候補をさらに読み込む") {
                        Task { await catalog.loadMoreModels(connectionID: form.connectionID) }
                    }
                    .disabled(catalog.isLoading)
                }
            }
            Text("画面を開いたときは保存済みの音声認識候補だけを読みます。「取得・更新」はモデル情報を照会しますが、音声の送信・認識は行いません。")
                .font(.caption).foregroundStyle(.secondary)
        }
    }

    @ViewBuilder private var modelDetails: some View {
        if let model = selectedModel {
            if let reason = TranscriptionModelForm.modelUnavailableReason(model) {
                Text(reason).font(.caption).foregroundStyle(.orange)
            }
            if let date = model.availabilityExpiresOn {
                LabeledContent("提供終了予定日", value: date).font(.caption)
                Text("アプリでは UTC の日付が予定日以降になったモデルを選択できません。公式の終了時刻を示すものではありません。")
                    .font(.caption).foregroundStyle(.secondary)
            }
            LabeledContent("音声単価", value: ProviderDisplay.price(model.billing.audioUSDPerMinute, unit: "分"))
            LabeledContent("単価確認時刻", value: model.billing.fetchedAt ?? "未確認")
            if model.modelID == "whisper-1" {
                Text("単語・発話区間の時刻付き結果を使います。この API の文字起こしだけで話者の実名は確定しません。")
                    .font(.caption).foregroundStyle(.secondary)
            } else if model.modelID == "gpt-4o-transcribe-diarize" {
                Text("時刻付きの匿名話者ラベルを使います。ラベルはトラック・区間ごとに扱い、実名や別区間の同一人物とはみなしません。既存の話者分離設定・マイク本人設定は維持します。")
                    .font(.caption).foregroundStyle(.secondary)
            }
        }
    }

    private var audioDisclosure: some View {
        DisclosureGroup("音声送信・課金・中断後の再開") {
            VStack(alignment: .leading, spacing: 6) {
                Text("マイクとシステム音声がある場合は別々に送信します。同じ会議でも2トラック分の API 利用となる場合があります。音声は最長10分の区間に分け、順番に処理します。")
                Text("取消は通信を切る操作であり、サービス側の処理や課金の停止を保証できません。結果不明の区間は自動再送しません。")
                Text("同じ入力・設定の成功区間は保存結果を再利用します。不明区間の再送には、文字起こしタブの「不明区間を再送」で対象を確認する操作が別に必要です。試行番号だけを増やしても再送できません。")
            }
            .font(.caption).foregroundStyle(.secondary)
        }
    }

    private func selectConnection(_ id: String) {
        guard !id.isEmpty else { form.selectConnection(nil); return }
        guard let candidate = catalog.connection(id), catalog.connectionUnavailableReason(candidate) == nil else { return }
        form.selectConnection(candidate)
    }

    private func selectModel(_ id: String) {
        guard !id.isEmpty else { form.selectModel(nil); return }
        guard let model = models.first(where: { $0.modelID == id }),
            TranscriptionModelForm.isTranscriptionModel(model) else { return }
        form.selectModel(model)
    }
}
