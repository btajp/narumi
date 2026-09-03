import NarumiMenuBarCore
import SwiftUI

struct MinutesModelSelectionView: View {
    private struct VerificationTarget {
        let connectionID: String
        let connectionRevision: Int
        let model: ProviderModelDescriptor
    }

    @Binding var form: MinutesModelForm
    let catalog: MinutesModelCatalogStore
    let externalSendPolicy: String
    var isProfile = false
    @State private var showNewAttemptConfirmation = false
    @State private var preparedNewAttempt = false
    @State private var verificationCandidate: VerificationTarget?

    private var connection: ProviderConnection? { catalog.connection(form.connectionID) }
    private var response: ListProviderModelsResponse? { catalog.catalogs[form.connectionID] }
    private var providerConnections: [ProviderConnection] { catalog.connections(for: form.provider) }
    private var models: [ProviderModelDescriptor] { response?.models ?? [] }
    private var selectedModel: ProviderModelDescriptor? {
        models.first { $0.modelID == form.modelID }
    }
    private var verificationCandidates: [ProviderModelDescriptor] {
        guard let connection else { return [] }
        return models.filter {
            catalog.isVerificationCandidate(
                connectionID: connection.connectionID,
                expectedRevision: connection.revision, model: $0)
        }
    }
    private var revisionChanged: Bool {
        connection.map { $0.revision != form.connectionRevision } ?? false
    }
    private var canUseConnection: Bool {
        guard catalog.supportedProviders.contains(form.provider), let connection,
            connection.providerID.rawValue == form.provider else { return false }
        return !revisionChanged && catalog.connectionUnavailableReason(connection) == nil
    }
    private var canEditParameters: Bool {
        guard !catalog.isLoading, canUseConnection, let selectedModel else { return false }
        return MinutesModelForm.isTextMinutesModel(selectedModel, provider: form.provider)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Picker("議事録の生成方法", selection: $form.mode) {
                ForEach(MinutesModelForm.Mode.allCases, id: \.self) { Text($0.title).tag($0) }
            }
            if form.mode == .selected {
                providerPicker
                Text(providerDisclosure).font(.caption)
                connectionPicker
                modelPicker
                parameterControls
                if let message = form.validationMessage(
                    connections: catalog.connections, catalog: response, externalSendPolicy: externalSendPolicy,
                    supportedProviders: catalog.supportedProviders, providers: catalog.providers) {
                    Text(message).font(.caption).foregroundStyle(.orange)
                }
                if let message = catalog.errorMessage {
                    Text(message).font(.caption).foregroundStyle(.red)
                }
                if let message = catalog.verificationNotice {
                    Text(message).font(.caption).foregroundStyle(.orange)
                }
                if ["claude-agent-sdk", "openai-api", "openai-compatible-api", "anthropic-api"]
                    .contains(form.provider) {
                    Text("取消は通信を切る操作です。サービス側の処理や課金の停止は保証できません。送信後に結果が不明になっても自動再送しません。")
                        .font(.caption).foregroundStyle(.secondary)
                }
                if form.provider == "openai-api" {
                    Text("応答保存を無効にして要求しますが、サービス側の不正利用監視などによるデータ保持がなくなることは保証しません。")
                        .font(.caption).foregroundStyle(.secondary)
                }
                newAttemptControls
                Text(isProfile
                     ? "保存だけでは生成しません。新しい録画・取り込みにこの設定を適用し、自動処理が有効な場合は処理時に上の接続へテキストを渡します。既存会議には適用しません。"
                     : "保存だけでは生成しません。保存後、議事録タブで処理内容と接続先を確認して再生成してください。")
                    .font(.caption).foregroundStyle(.secondary)
            } else {
                Text("議事録にも従来の LLM 設定を使います。保存すると議事録用の接続・モデルの個別選択を解除します。")
                    .font(.caption).foregroundStyle(.secondary)
            }
        }
        .task(id: form.catalogReadIdentity + "/" + catalog.supportedProviders.joined(separator: ",")) {
            guard form.mode == .selected else { return }
            await catalog.loadCachedCatalog(connectionID: form.connectionID, selectedModelID: form.modelID)
        }
        .confirmationDialog(
            "このモデルへ固定テスト文を送信しますか？",
            isPresented: Binding(
                get: { verificationCandidate != nil },
                set: { if !$0 { verificationCandidate = nil } }),
            titleVisibility: .visible
        ) {
            Button("送信してモデルを検証") {
                guard let candidate = verificationCandidate else { return }
                verificationCandidate = nil
                guard form.connectionID == candidate.connectionID,
                    form.connectionRevision == candidate.connectionRevision,
                    connection?.connectionID == candidate.connectionID,
                    connection?.revision == candidate.connectionRevision,
                    catalog.isVerificationCandidate(
                        connectionID: candidate.connectionID,
                        expectedRevision: candidate.connectionRevision,
                        model: candidate.model) else {
                    catalog.rejectChangedVerificationConfirmation()
                    return
                }
                Task {
                    await catalog.verifyModel(
                        connectionID: candidate.connectionID,
                        expectedRevision: candidate.connectionRevision,
                        expectedModel: candidate.model,
                        confirmation: .sendTestPromptAndMayCharge)
                }
            }
            Button("キャンセル", role: .cancel) { verificationCandidate = nil }
        } message: {
            Text("会議データではなく固定の非機密テスト文を 1 回送信します。送信先で API 利用料が発生する場合があります。結果が不明でも自動再送しません。再実行は、状態と課金を確認してから改めて操作してください。")
        }
    }

    private var providerPicker: some View {
        VStack(alignment: .leading, spacing: 6) {
            Picker("議事録プロバイダ", selection: Binding(
                get: { catalog.supportedProviders.contains(form.provider) ? form.provider : "" },
                set: { form.selectProvider($0) })) {
                Text("プロバイダを選択").tag("")
                ForEach(catalog.supportedProviders, id: \.self) { provider in
                    Text(providerName(provider)).tag(provider)
                }
            }
            .disabled(catalog.isLoading)
            if !form.provider.isEmpty, !catalog.supportedProviders.contains(form.provider) {
                Text("保存済み: \(providerName(form.provider))（このサーバーでは議事録生成に利用できません）")
                    .font(.caption).foregroundStyle(.orange)
            }
            if catalog.supportedProviders.isEmpty, !catalog.isLoading {
                Text("対応する議事録プロバイダを確認できません。アプリとサーバーを更新し、接続一覧を再読込してください。")
                    .font(.caption).foregroundStyle(.orange)
            }
        }
    }

    private var connectionPicker: some View {
        VStack(alignment: .leading, spacing: 6) {
            Picker("保存済み接続", selection: Binding(
                get: { form.connectionID },
                set: { id in selectConnection(id) })) {
                Text("接続を選択").tag("")
                ForEach(providerConnections, id: \.connectionID) { candidate in
                    let reason = catalog.connectionUnavailableReason(candidate)
                    Text("\(candidate.displayName)（rev \(candidate.revision)）" + (reason.map { " — \($0)" } ?? ""))
                        .tag(candidate.connectionID)
                        .disabled(reason != nil)
                }
                if !form.connectionID.isEmpty, !providerConnections.contains(where: { $0.connectionID == form.connectionID }) {
                    Text("保存済み: \(form.connectionID)（利用不可）").tag(form.connectionID)
                        .disabled(true)
                }
            }
            .disabled(catalog.isLoading || !catalog.supportedProviders.contains(form.provider))
            if let connection, let reason = catalog.connectionUnavailableReason(connection) {
                Text(reason).font(.caption).foregroundStyle(.orange)
            }
            if revisionChanged {
                Text("保存済み rev \(form.connectionRevision ?? 0) → 現在 rev \(connection?.revision ?? 0)")
                    .font(.caption).foregroundStyle(.orange)
                Button("変更後の接続を選び直す") { selectConnection(connection?.connectionID ?? "") }
                    .disabled(catalog.isLoading || connection.map {
                        $0.providerID.rawValue != form.provider || catalog.connectionUnavailableReason($0) != nil
                    } != false)
            }
            HStack {
                Button("接続一覧を再読込") {
                    Task { await catalog.loadCachedCatalog(connectionID: form.connectionID, selectedModelID: form.modelID) }
                }
                .disabled(catalog.isLoading)
                if catalog.isLoading { ProgressView().controlSize(.small) }
            }
            if !form.provider.isEmpty, providerConnections.isEmpty, !catalog.isLoading {
                Text("この画面を閉じて上部の「AI 接続」で \(providerName(form.provider)) の接続を保存し、実行環境の準備と認証・接続確認を完了してください。")
                    .font(.caption).foregroundStyle(.secondary)
            }
        }
    }

    private var modelPicker: some View {
        VStack(alignment: .leading, spacing: 6) {
            Picker("議事録モデル", selection: Binding(
                get: { form.modelID },
                set: { id in selectModel(id) })) {
                Text("モデルを選択").tag("")
                ForEach(models, id: \.modelID) { model in
                    let reason = MinutesModelForm.modelUnavailableReason(model, provider: form.provider)
                    Text("\(model.displayName)（\(model.modelID)）" + (reason.map { " — \($0)" } ?? ""))
                        .tag(model.modelID)
                        .disabled(reason != nil)
                }
                if !form.modelID.isEmpty, !models.contains(where: { $0.modelID == form.modelID }) {
                    Text("保存済み: \(form.modelID)（候補未確認）").tag(form.modelID)
                        .disabled(true)
                }
            }
            .disabled(catalog.isLoading || !canUseConnection)
            if let selectedModel, let reason = MinutesModelForm.modelUnavailableReason(selectedModel, provider: form.provider) {
                Text(reason).font(.caption).foregroundStyle(.orange)
            }
            if let date = selectedModel?.availabilityExpiresOn {
                LabeledContent("提供終了予定日", value: date)
                    .font(.caption)
            }
            HStack {
                Button("モデル候補を取得・更新") {
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
            if let providerID = connection?.providerID,
                [ProviderID.claudeAgentSDK, .openAICompatibleAPI].contains(providerID) {
                ForEach(verificationCandidates, id: \.modelID) { model in
                    Button("「\(model.displayName)」を検証…") {
                        guard let connection else { return }
                        verificationCandidate = VerificationTarget(
                            connectionID: connection.connectionID,
                            connectionRevision: connection.revision, model: model)
                    }
                        .disabled(catalog.isLoading || !canUseConnection)
                }
                if !verificationCandidates.isEmpty {
                    Text("モデル一覧だけでは生成対応を確認できません。選択可能にするには、モデルごとに固定テスト文の送信と課金可能性を確認して検証します。自動検証は行いません。")
                        .font(.caption).foregroundStyle(.secondary)
                }
                if models.contains(where: {
                    $0.availability == .unverified && !verificationCandidates.contains($0)
                }) {
                    Text("LLM・テキスト入出力・API 課金区分・安全に設定できるパラメータを確認できない候補には、課金を伴う検証を実行しません。")
                        .font(.caption).foregroundStyle(.secondary)
                }
            }
            Text("画面を開いたときは保存済み候補だけを読みます。「取得・更新」はモデル一覧を通信しますが、会議内容は送信しません。")
                .font(.caption).foregroundStyle(.secondary)
        }
    }

    @ViewBuilder private var parameterControls: some View {
        if form.allowsReasoningEffort {
            if !MinutesModelForm.reasoningOptions(selectedModel).isEmpty || !form.reasoningEffort.isEmpty {
                reasoningPicker
            } else if selectedModel != nil {
                Text("このモデルで選択できる推論量は確認されていません。")
                    .font(.caption).foregroundStyle(.secondary)
            }
        }
        if form.allowsMaxTokens {
            TextField("出力上限（トークン）", text: $form.maxTokensText,
                      prompt: Text("空欄: アプリ既定の \(MinutesModelForm.defaultOutputLimit(selectedModel))"))
                .disabled(!canEditParameters)
            if let selectedModel {
                LabeledContent("モデルの出力上限", value: selectedModel.maxOutputTokens.map { "\($0) tokens" } ?? "未確認")
                LabeledContent("今回の要求上限", value: form.effectiveOutputLimit(selectedModel).map { "\($0) tokens" } ?? "入力を確認してください")
            }
            Text("1〜32,768 の整数を入力します。空欄では 4,096 と確認済みのモデル上限の小さい方を使います。モデル上限が不明でも、4,096 はアプリの既定値として扱います。")
                .font(.caption).foregroundStyle(.secondary)
            Text("1 回の生成要求の出力上限です。分割処理では複数回要求するため、生成全体の利用量や金額の上限ではありません。")
                .font(.caption).foregroundStyle(.secondary)
        } else if form.provider == "codex-app-server" {
            Text("Codex では出力トークン数の上限を指定できません。利用枠の消費量の上限は保証しません。")
                .font(.caption).foregroundStyle(.secondary)
        } else if form.provider == "claude-agent-sdk" {
            Text("Claude Agent SDK では出力トークン数の上限を指定しません。API 利用量や金額の上限は保証しません。")
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
                    .disabled(true)
            }
        }
        .disabled(!canEditParameters)
    }

    private var newAttemptControls: some View {
        DisclosureGroup("前回の結果が不明・新しい試行が必要なとき") {
            VStack(alignment: .leading, spacing: 6) {
                Text("同じ入力・選択では保存済みの結果を再利用します。結果不明の処理は自動再実行しません。")
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
                        Text(newAttemptWarning + " 過去の議事録は保持します。この操作だけでは保存・送信しません。")
                    }
                if preparedNewAttempt {
                    Text("試行番号を変更しました。このフォームを保存し、議事録タブで再生成してください。")
                        .foregroundStyle(.orange)
                }
            }
            .font(.caption)
        }
    }

    private func selectConnection(_ id: String) {
        guard !id.isEmpty else { form.selectConnection(nil); return }
        guard let candidate = catalog.connection(id), candidate.providerID.rawValue == form.provider,
            catalog.connectionUnavailableReason(candidate) == nil else { return }
        form.selectConnection(candidate)
    }

    private func selectModel(_ id: String) {
        guard !id.isEmpty else { form.selectModel(nil); return }
        guard let model = models.first(where: { $0.modelID == id }),
            MinutesModelForm.isTextMinutesModel(model, provider: form.provider) else { return }
        form.selectModel(model)
    }

    private func providerName(_ provider: String) -> String {
        ProviderID(rawValue: provider).map { ProviderDisplay.name($0) } ?? provider
    }

    private var providerDisclosure: String {
        switch form.provider {
        case "codex-app-server":
            return "送信先: OpenAI（https://chatgpt.com）。テキストを送信し、ChatGPT の利用枠を使います。subscription_ok または api_ok の明示が必要です。API 課金へは切り替えません。"
        case "openai-api":
            return "送信先: OpenAI（https://api.openai.com/v1/responses）。テキスト送信と従量課金を許可する api_ok の明示が必要です。ChatGPT の利用枠とは別の API 課金です。"
        case "openai-compatible-api":
            let endpoint = connection?.endpoint ?? "接続を選ぶと表示します"
            let surface = connection?.apiSurface == .chatCompletions ? "/chat/completions" : "/responses"
            return "送信先: 保存した OpenAI互換API（\(endpoint)\(surface)）。ローカル URL でも外部へ中継する可能性があるため api_ok が必要です。失敗時に別形式や別プロバイダへ切り替えません。"
        case "claude-agent-sdk":
            return "送信先: Claude Agent SDK から Anthropic API（https://api.anthropic.com）。テキスト送信と従量 API 課金を許可する api_ok が必要です。Claude のサブスクリプションログインは使用しません。"
        case "anthropic-api":
            return "送信先: Anthropic（https://api.anthropic.com）。テキスト送信と従量課金を許可する api_ok の明示が必要です。Claude のサブスクリプションとは別の API 課金です。"
        case "ollama":
            return "接続先: この Mac の Ollama（\(connection?.endpoint ?? "接続を選ぶと表示します")）。ローカル実行を確認したモデルだけを使用します。local_only で利用でき、API 課金のプロバイダへは切り替えません。"
        default:
            return "プロバイダを選ぶと、テキストの送信先と利用条件を表示します。生成可能かどうかは接続・実行環境・モデル候補の状態で確認します。"
        }
    }

    private var newAttemptWarning: String {
        switch form.provider {
        case "claude-agent-sdk", "openai-api", "openai-compatible-api", "anthropic-api":
            return "前の試行がサービス側で完了している可能性があります。新しく試すと API 利用料が重複して発生する場合があります。"
        case "codex-app-server":
            return "前の試行がサービス側で完了している可能性があります。新しく試すと ChatGPT の利用枠を重複して消費する場合があります。"
        default:
            return "前の試行が完了している可能性があります。新しく試すとローカルの生成処理を改めて実行します。"
        }
    }
}
