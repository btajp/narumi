import Foundation

/// Draft-only API selection. Language and local engine settings belong to the shared form.
public struct TranscriptionModelForm: Equatable, Sendable {
    public enum Mode: String, CaseIterable, Sendable {
        case local, selected

        public var title: String { self == .local ? "ローカルの設定を使う" : "OpenAI API を使う" }
    }

    public var mode: Mode = .local
    public private(set) var provider = "openai-api"
    public private(set) var connectionID = ""
    public private(set) var connectionRevision: Int?
    public private(set) var modelID = ""
    public private(set) var cacheEpoch = 0

    public init(selection: TranscriptionModelSelection? = nil) {
        guard let selection else { return }
        mode = .selected
        provider = selection.provider
        connectionID = selection.connectionID
        connectionRevision = selection.connectionRevision
        modelID = selection.modelID
        cacheEpoch = selection.cacheEpoch
    }

    public mutating func selectConnection(_ connection: ProviderConnection?) {
        guard connection == nil || connection?.providerID == .openaiAPI else { return }
        let changed = connectionID != connection?.connectionID || connectionRevision != connection?.revision
        connectionID = connection?.connectionID ?? ""
        connectionRevision = connection?.revision
        if let connection { provider = connection.providerID.rawValue }
        if changed { modelID = "" }
    }

    public mutating func selectModel(_ model: ProviderModelDescriptor?) {
        modelID = model?.modelID ?? ""
    }

    public var selection: TranscriptionModelSelection? {
        guard mode == .selected, let connectionRevision else { return nil }
        let value = TranscriptionModelSelection(
            provider: provider, connectionID: connectionID, connectionRevision: connectionRevision,
            modelID: modelID, cacheEpoch: cacheEpoch)
        return value.isWellFormed ? value : nil
    }

    public var catalogReadIdentity: String {
        "\(mode.rawValue)/\(provider)/\(connectionID)/\(connectionRevision ?? 0)/\(modelID)"
    }

    public static func isTranscriptionModel(_ model: ProviderModelDescriptor) -> Bool {
        modelUnavailableReason(model) == nil
    }

    public static func modelUnavailableReason(_ model: ProviderModelDescriptor, at now: Date = Date()) -> String? {
        guard TranscriptionModelSelection.modelIDs.contains(model.modelID) else {
            return "この音声モデルの時刻付き文字起こしには対応していません。"
        }
        guard model.availability == .available else {
            return ProviderDisplay.reason(model.reason) ?? ProviderDisplay.availability(model.availability)
        }
        guard !model.isAvailabilityExpired(at: now) else { return "このモデルは確認済みの提供終了日を迎えています。" }
        guard model.roles.contains(.transcription), model.inputModalities.contains(.audio),
            model.outputModalities.contains(.text), model.source == .providerAPI else {
            return "音声入力と文字起こし出力への対応を確認できません。"
        }
        let timestamp: ProviderTimestampSupport = model.modelID == "whisper-1" ? .word : .diarizedSegment
        guard model.timestampSupport == timestamp else {
            return "このモデルに必要な時刻付き結果への対応を確認できません。"
        }
        guard model.billing.kind == .api else { return "この接続の API 課金区分を確認できません。" }
        guard model.parameterSchema.properties.isEmpty, model.parameterSchema.required.isEmpty,
            !model.parameterSchema.additionalProperties else {
            return "API 文字起こしでは追加パラメータを指定できません。対応する候補を再取得してください。"
        }
        return nil
    }

    public static func connectionUnavailableReason(
        _ connection: ProviderConnection, providers: [ProviderDescriptor]? = nil
    ) -> String? {
        guard connection.providerID == .openaiAPI, connection.authMethod == .apiKey,
            connection.endpoint == ProviderConnectionSettings.openaiEndpoint else {
            return "OpenAI API の保存済み接続を選んでください。Codex のログインは使用しません。"
        }
        guard connection.enabled else { return "接続が無効です。AI 接続で有効にしてください。" }
        guard connection.credentialPresent else { return "AI 接続で、この接続の API キーを保存してください。" }
        guard connection.authState == .authenticated, connection.activeAuth == nil else {
            return "AI 接続で、この接続の認証・接続確認を完了してください。"
        }
        if let providers {
            guard let descriptor = providers.first(where: { $0.providerID == .openaiAPI }),
                descriptor.runtime.state == .ready, descriptor.roles.contains(.transcription) else {
                return "AI 接続で、音声認識に対応する OpenAI アダプタを準備・確認してください。"
            }
        }
        return nil
    }

    public func validationMessage(
        connections: [ProviderConnection], catalog: ListProviderModelsResponse?, externalSendPolicy: String,
        language: String, supportedProviders: [String] = TranscriptionModelSelection.providers,
        providers: [ProviderDescriptor]? = nil
    ) -> String? {
        guard mode == .selected else { return nil }
        guard supportedProviders.contains(provider) else {
            return "このサーバーは API 文字起こしの選択に対応していません。アプリを更新してください。"
        }
        guard externalSendPolicy == "api_ok" else {
            return "音声の外部送信と API 課金を許可するには、外部送信ポリシーで api_ok を明示的に選んでください。"
        }
        guard Self.isSupportedLanguage(language) else {
            return "API 文字起こしの言語は auto または小文字2文字の ISO 639-1 コードを指定してください（例: ja、en）。"
        }
        guard !connectionID.isEmpty else { return "AI 接続に保存した OpenAI API 接続を選んでください。" }
        guard let connection = connections.first(where: { $0.connectionID == connectionID }) else {
            return "選択した接続が見つかりません。接続一覧を再読込してください。"
        }
        if let reason = Self.connectionUnavailableReason(connection, providers: providers) { return reason }
        guard connectionRevision == connection.revision else {
            return "接続の設定が変更されています。「変更後の接続を選び直す」で確認し、音声認識モデルを選び直してください。"
        }
        guard let catalog, catalog.connectionID == connectionID,
            catalog.connectionRevision == connectionRevision, catalog.catalogState == .ready else {
            return "音声認識モデルをまだ確認できません。「音声認識の候補を取得・更新」で候補を取得してください。"
        }
        guard let model = catalog.models.first(where: { $0.modelID == modelID }) else {
            return "取得済みの候補から、時刻付き文字起こしに対応するモデルを選んでください。"
        }
        if let reason = Self.modelUnavailableReason(model) { return reason }
        return selection == nil ? "接続・音声認識モデルの選択を確認してください。" : nil
    }
}
