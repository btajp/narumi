import Foundation

/// Draft-only state. Choosing a connection or model never writes config or starts generation.
public struct MinutesModelForm: Equatable, Sendable {
    public enum Mode: String, CaseIterable, Sendable {
        case legacy, codex

        public var title: String { self == .legacy ? "従来設定" : "Codex App Server" }
    }

    public var mode: Mode = .legacy
    public private(set) var connectionID = ""
    public private(set) var connectionRevision: Int?
    public private(set) var modelID = ""
    public var reasoningEffort = ""
    public private(set) var cacheEpoch = 0

    public init(selection: CodexMinutesSelection? = nil) {
        guard let selection else { return }
        mode = .codex
        connectionID = selection.connectionID
        connectionRevision = selection.connectionRevision
        modelID = selection.modelID
        reasoningEffort = selection.parameters.reasoningEffort ?? ""
        cacheEpoch = selection.cacheEpoch
    }

    public mutating func selectConnection(_ connection: ProviderConnection?) {
        let changed = connectionID != connection?.connectionID || connectionRevision != connection?.revision
        connectionID = connection?.connectionID ?? ""
        connectionRevision = connection?.revision
        if changed {
            modelID = ""
            reasoningEffort = ""
        }
    }

    public mutating func selectModel(_ model: ProviderModelDescriptor?) {
        guard modelID != model?.modelID else { return }
        modelID = model?.modelID ?? ""
        reasoningEffort = ""
    }

    /// Only an explicit confirmation may advance this value. Saving does not send anything.
    public mutating func prepareNewAttempt() {
        guard cacheEpoch < Int.max else { return }
        cacheEpoch += 1
    }

    public var selection: CodexMinutesSelection? {
        guard mode == .codex, let connectionRevision else { return nil }
        let value = CodexMinutesSelection(
            connectionID: connectionID, connectionRevision: connectionRevision, modelID: modelID,
            reasoningEffort: reasoningEffort.isEmpty ? nil : reasoningEffort, cacheEpoch: cacheEpoch)
        return value.isWellFormed ? value : nil
    }

    /// A different saved model may require another cached catalog page, even on the same connection.
    public var catalogReadIdentity: String {
        "\(mode.rawValue)/\(connectionID)/\(connectionRevision ?? 0)/\(modelID)"
    }

    public static func isTextMinutesModel(_ model: ProviderModelDescriptor) -> Bool {
        model.availability == .available && model.roles.contains(.llm)
            && model.inputModalities.contains(.text) && model.outputModalities.contains(.text)
            && model.billing.kind == .subscription
    }

    public static func reasoningOptions(_ model: ProviderModelDescriptor?) -> [String] {
        guard let parameter = model?.parameterSchema.properties["reasoning_effort"], parameter.type == .string else {
            return []
        }
        return parameter.enumValues?.compactMap {
            if case .string(let value) = $0 { return value }
            return nil
        } ?? []
    }

    public func validationMessage(
        connections: [ProviderConnection], catalog: ListProviderModelsResponse?, externalSendPolicy: String
    ) -> String? {
        guard mode == .codex else { return nil }
        guard ["subscription_ok", "api_ok"].contains(externalSendPolicy) else {
            return "Codex は OpenAI にテキストを送信します。外部送信ポリシーで subscription_ok または api_ok を明示的に選んでください。"
        }
        guard !connectionID.isEmpty else { return "AI 接続に保存した Codex の接続を選んでください。" }
        guard let connection = connections.first(where: { $0.connectionID == connectionID }),
            connection.providerID == .codexAppServer, connection.enabled else {
            return "この接続は削除されたか無効です。AI 接続で確認し、有効な Codex の接続を選んでください。"
        }
        guard connectionRevision == connection.revision else {
            return "接続の設定が変更されています。「変更後の接続を選び直す」で確認し、モデルを選び直してください。"
        }
        guard connection.authMethod == .chatgpt, connection.authState == .authenticated,
            connection.activeAuth == nil else {
            return "AI 接続で、この Codex 接続への ChatGPT ログインを完了してください。"
        }
        guard let catalog, catalog.connectionID == connectionID,
            catalog.connectionRevision == connectionRevision, catalog.catalogState == .ready else {
            return "利用できるモデルをまだ確認できません。「モデル候補を取得・更新」で候補を取得してください。"
        }
        guard let model = catalog.models.first(where: { $0.modelID == modelID }), Self.isTextMinutesModel(model) else {
            return "取得済みの候補から、利用できるテキスト議事録モデルを選んでください。"
        }
        guard model.parameterSchema.required.allSatisfy({ $0 == "reasoning_effort" }) else {
            return "このモデルには未対応の必須パラメータがあります。別のモデルを選んでください。"
        }
        if reasoningEffort.isEmpty {
            if model.parameterSchema.required.contains("reasoning_effort"),
                model.parameterSchema.properties["reasoning_effort"]?.defaultValue == nil {
                return "このモデルの推論量を選んでください。"
            }
        } else if !Self.reasoningOptions(model).contains(reasoningEffort) {
            return "保存された推論量はこのモデルで利用できません。候補から選び直してください。"
        }
        return selection == nil ? "接続・モデルの選択を確認してください。" : nil
    }
}
