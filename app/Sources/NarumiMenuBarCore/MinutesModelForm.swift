import Foundation

/// Draft-only state. Choosing a connection or model never writes config or starts generation.
public struct MinutesModelForm: Equatable, Sendable {
    public enum Mode: String, CaseIterable, Sendable {
        case legacy, selected

        public var title: String { self == .legacy ? "従来設定" : "接続とモデルを指定" }
    }

    public var mode: Mode = .legacy
    public private(set) var provider = ""
    public private(set) var connectionID = ""
    public private(set) var connectionRevision: Int?
    public private(set) var modelID = ""
    public var reasoningEffort = ""
    public var maxTokensText = ""
    public private(set) var cacheEpoch = 0

    public init(selection: MinutesModelSelection? = nil) {
        guard let selection else { return }
        mode = .selected
        provider = selection.provider
        connectionID = selection.connectionID
        connectionRevision = selection.connectionRevision
        modelID = selection.modelID
        reasoningEffort = selection.parameters.reasoningEffort ?? ""
        maxTokensText = selection.parameters.maxTokens.map(String.init) ?? ""
        cacheEpoch = selection.cacheEpoch
    }

    public mutating func selectProvider(_ provider: String) {
        guard self.provider != provider else { return }
        self.provider = MinutesModelSelection.providers.contains(provider) ? provider : ""
        connectionID = ""
        connectionRevision = nil
        resetModel()
    }

    public mutating func selectConnection(_ connection: ProviderConnection?) {
        let changed = connectionID != connection?.connectionID || connectionRevision != connection?.revision
            || (connection.map { provider != $0.providerID.rawValue } ?? false)
        connectionID = connection?.connectionID ?? ""
        connectionRevision = connection?.revision
        if let connection { provider = connection.providerID.rawValue }
        if changed { resetModel() }
    }

    public mutating func selectModel(_ model: ProviderModelDescriptor?) {
        guard modelID != model?.modelID else { return }
        resetModel()
        modelID = model?.modelID ?? ""
    }

    private mutating func resetModel() {
        modelID = ""
        reasoningEffort = ""
        maxTokensText = ""
    }

    /// Only an explicit confirmation may advance this value. Saving does not send anything.
    public mutating func prepareNewAttempt() {
        guard cacheEpoch < Int.max else { return }
        cacheEpoch += 1
    }

    public var selection: MinutesModelSelection? {
        guard mode == .selected, let connectionRevision,
            maxTokensText.isEmpty || parsedMaxTokens != nil else { return nil }
        let value = MinutesModelSelection(
            provider: provider, connectionID: connectionID, connectionRevision: connectionRevision, modelID: modelID,
            reasoningEffort: reasoningEffort.isEmpty ? nil : reasoningEffort,
            maxTokens: parsedMaxTokens, cacheEpoch: cacheEpoch)
        return value.isWellFormed ? value : nil
    }

    public var parsedMaxTokens: Int? {
        guard !maxTokensText.isEmpty, maxTokensText.utf8.allSatisfy({ (48...57).contains($0) }),
            let value = Int(maxTokensText), (1...32768).contains(value) else { return nil }
        return value
    }

    public var allowsReasoningEffort: Bool { ["codex-app-server", "openai-api"].contains(provider) }
    public var allowsMaxTokens: Bool { ["openai-api", "anthropic-api", "ollama"].contains(provider) }

    /// A different saved model may require another cached catalog page, even on the same connection.
    public var catalogReadIdentity: String {
        "\(mode.rawValue)/\(provider)/\(connectionID)/\(connectionRevision ?? 0)/\(modelID)"
    }

    public static func isTextMinutesModel(_ model: ProviderModelDescriptor, provider: String) -> Bool {
        modelUnavailableReason(model, provider: provider) == nil
    }

    public static func modelUnavailableReason(_ model: ProviderModelDescriptor, provider: String) -> String? {
        guard MinutesModelSelection.providers.contains(provider) else { return "このプロバイダの議事録生成には未対応です。" }
        guard model.availability == .available else {
            return ProviderDisplay.reason(model.reason) ?? ProviderDisplay.availability(model.availability)
        }
        guard !model.availabilityExpired else { return "このモデルは確認済みの提供期限を過ぎています。" }
        guard model.roles.contains(.llm), model.inputModalities.contains(.text), model.outputModalities.contains(.text) else {
            return "テキスト議事録への対応を確認できません。"
        }
        let billing: ProviderBillingKind = provider == "codex-app-server" ? .subscription : (provider == "ollama" ? .local : .api)
        guard model.billing.kind == billing else { return "この接続で使用する課金区分を確認できません。" }
        let allowed = provider == "codex-app-server" ? ["reasoning_effort"]
            : (provider == "openai-api" ? ["reasoning_effort", "max_tokens"] : ["max_tokens"])
        guard model.parameterSchema.required.allSatisfy({ allowed.contains($0) }) else {
            return "このモデルには未対応の必須パラメータがあります。"
        }
        if provider != "codex-app-server", model.parameterSchema.properties["max_tokens"]?.type != .integer {
            return "このモデルの出力上限設定への対応を確認できません。"
        }
        return nil
    }

    public static func reasoningOptions(_ model: ProviderModelDescriptor?) -> [String] {
        guard let parameter = model?.parameterSchema.properties["reasoning_effort"], parameter.type == .string else {
            return []
        }
        return parameter.enumValues?.compactMap {
            if case .string(let value) = $0,
                value.range(of: #"\A[a-z][a-z0-9_-]{0,31}\z"#, options: .regularExpression) != nil { return value }
            return nil
        } ?? []
    }

    public func validationMessage(
        connections: [ProviderConnection], catalog: ListProviderModelsResponse?, externalSendPolicy: String,
        supportedProviders: [String] = MinutesModelSelection.providers, providers: [ProviderDescriptor]? = nil
    ) -> String? {
        guard mode == .selected else { return nil }
        guard supportedProviders.contains(provider) else {
            return "このサーバーが対応する議事録プロバイダを選んでください。利用できない場合はアプリを更新してください。"
        }
        if let message = policyValidationMessage(externalSendPolicy) { return message }
        guard !connectionID.isEmpty else { return "AI 接続に保存した接続を選んでください。" }
        guard let connection = connections.first(where: { $0.connectionID == connectionID }),
            connection.providerID.rawValue == provider else { return "この接続は削除されたか選択したプロバイダと一致しません。" }
        if let message = Self.connectionUnavailableReason(connection, providers: providers) { return message }
        guard connectionRevision == connection.revision else {
            return "接続の設定が変更されています。「変更後の接続を選び直す」で確認し、モデルを選び直してください。"
        }
        guard let catalog, catalog.connectionID == connectionID,
            catalog.connectionRevision == connectionRevision, catalog.catalogState == .ready else {
            return "利用できるモデルをまだ確認できません。「モデル候補を取得・更新」で候補を取得してください。"
        }
        guard let model = catalog.models.first(where: { $0.modelID == modelID }) else {
            return "取得済みの候補から、利用できるテキスト議事録モデルを選んでください。"
        }
        if let reason = Self.modelUnavailableReason(model, provider: provider) { return reason }
        if let reason = parameterValidationMessage(model) { return reason }
        return selection == nil ? "接続・モデルの選択を確認してください。" : nil
    }

    public static func connectionUnavailableReason(
        _ connection: ProviderConnection, providers: [ProviderDescriptor]? = nil
    ) -> String? {
        guard MinutesModelSelection.providers.contains(connection.providerID.rawValue) else {
            return "このプロバイダの議事録生成には未対応です。"
        }
        guard connection.enabled else { return "接続が無効です。AI 接続で有効にしてください。" }
        let settings = ProviderConnectionSettings(connection: connection)
        guard let endpoint = connection.endpoint, settings.isEndpointValid, endpoint == settings.normalizedEndpoint else {
            return "接続の送信先を確認できません。AI 接続で公式 API またはローカル接続先を選び直してください。"
        }
        guard connection.authMethod == connection.providerID.supportedAuthMethod,
            connection.authState == .authenticated, connection.activeAuth == nil else {
            return connection.providerID == .codexAppServer
                ? "AI 接続で、この Codex 接続への ChatGPT ログインを完了してください。"
                : "AI 接続で、この接続の認証・接続確認を完了してください。"
        }
        if connection.authMethod == .apiKey, !connection.credentialPresent {
            return "AI 接続で、この接続の API キーを保存して接続確認を行ってください。"
        }
        if let providers {
            guard let descriptor = providers.first(where: { $0.providerID == connection.providerID }),
                descriptor.roles.contains(.llm), descriptor.runtime.state == .ready else {
                return "AI 接続で、このプロバイダの実行環境を準備・確認してください。"
            }
        }
        return nil
    }

    private func policyValidationMessage(_ policy: String) -> String? {
        switch provider {
        case "codex-app-server":
            return ["subscription_ok", "api_ok"].contains(policy) ? nil
                : "Codex は OpenAI にテキストを送信します。外部送信ポリシーで subscription_ok または api_ok を明示的に選んでください。"
        case "openai-api", "anthropic-api":
            return policy == "api_ok" ? nil
                : "API へのテキスト送信と従量課金を許可するには、外部送信ポリシーで api_ok を明示的に選んでください。"
        case "ollama":
            return ["local_only", "subscription_ok", "api_ok"].contains(policy) ? nil : "外部送信ポリシーを確認してください。"
        default: return "このプロバイダの議事録生成には未対応です。"
        }
    }
}
