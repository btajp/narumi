import Foundation

/// Shared by the meeting and profile editors. An empty policy retains the original policy.
public struct ProcessingConfigurationForm: Equatable, Sendable {
    public var transcriptionEngine: String
    public var diarizationEngine: String
    public var llmProvider: String
    public var externalSendPolicy: String
    public var language: String
    public var selfName: String
    public var vocabHintsText: String
    private var ensembleMode: Bool
    public var minutesModel: MinutesModelForm
    public var minutesEnsemble: MinutesEnsembleForm
    public var transcriptionModel: TranscriptionModelForm
    public let originalConfig: MeetingConfig
    private let originalPolicy: String
    private let originalLanguage: String

    public init(config: MeetingConfig = MeetingConfig()) {
        transcriptionEngine = config.transcriptionEngine ?? ""
        diarizationEngine = config.diarizationEngine ?? ""
        llmProvider = config.llmProvider ?? ""
        externalSendPolicy = config.externalSendPolicy ?? ""
        language = config.language ?? ""
        selfName = config.selfName ?? ""
        vocabHintsText = (config.vocabHints ?? []).joined(separator: "\n")
        ensembleMode = config.minutesEnsemble != nil
        minutesModel = MinutesModelForm(selection: config.minutesModel)
        minutesEnsemble = MinutesEnsembleForm(selection: config.minutesEnsemble)
        transcriptionModel = TranscriptionModelForm(selection: config.transcriptionModel)
        originalConfig = config
        originalPolicy = config.externalSendPolicy ?? "local_only"
        originalLanguage = config.language ?? "ja"
    }

    public var effectiveExternalSendPolicy: String {
        externalSendPolicy.isEmpty ? originalPolicy : externalSendPolicy
    }

    public var effectiveLanguage: String { language.isEmpty ? originalLanguage : language }

    public var vocabHints: [String] {
        vocabHintsText.split(whereSeparator: \.isNewline)
            .map { $0.trimmingCharacters(in: .whitespaces) }.filter { !$0.isEmpty }
    }

    public var minutesGenerationMode: MinutesGenerationMode {
        ensembleMode ? .ensemble : (minutesModel.mode == .selected ? .single : .legacy)
    }

    public mutating func selectMinutesGenerationMode(_ mode: MinutesGenerationMode) {
        ensembleMode = mode == .ensemble
        if mode == .legacy { minutesModel.mode = .legacy }
        if mode == .single { minutesModel.mode = .selected }
        if mode == .ensemble { minutesEnsemble.activateEditing() }
    }

    public func makeUpdate(
        supportsMinutesModel: Bool = true, supportedProviders: [String] = MinutesModelSelection.providers,
        supportsMinutesEnsembleWire: Bool = false, canExecuteMinutesEnsemble: Bool? = nil,
        supportsTranscriptionModel: Bool = true,
        supportedTranscriptionProviders: [String] = TranscriptionModelSelection.providers
    ) throws -> ProcessingConfigurationUpdate {
        guard supportsMinutesModel || minutesGenerationMode != .single else {
            throw ConfigurationFormFailure(message: "このサーバーは議事録モデル選択に対応していません。アプリを更新してください。")
        }
        guard minutesGenerationMode != .single
            || (minutesModel.selection != nil && supportedProviders.contains(minutesModel.provider)) else {
            throw ConfigurationFormFailure(message: "対応するプロバイダの接続・モデルと、利用できるパラメータを選んでください。")
        }
        let ensembleReady = canExecuteMinutesEnsemble ?? supportsMinutesEnsembleWire
        guard ensembleReady || minutesGenerationMode != .ensemble else {
            throw ConfigurationFormFailure(message: "このサーバーは複数案の生成と統合に対応していません。アプリを更新してください。")
        }
        if minutesGenerationMode == .ensemble {
            guard let ensemble = minutesEnsemble.selection,
                ensemble.generators.allSatisfy({ supportedProviders.contains($0.selection.provider) }),
                supportedProviders.contains(ensemble.synthesizer.provider) else {
                throw ConfigurationFormFailure(message: minutesEnsemble.structuralValidationMessage
                    ?? "対応する生成担当と統合担当を選んでください。")
            }
            let participants = ensemble.generators.map(\.selection) + [ensemble.synthesizer]
            guard Self.policyAllows(participants: participants, policy: effectiveExternalSendPolicy) else {
                throw ConfigurationFormFailure(message: "複数案の全担当に必要な外部送信ポリシーを明示してください。API 接続には api_ok が必要です。")
            }
        }
        guard supportsTranscriptionModel || transcriptionModel.mode == .local else {
            throw ConfigurationFormFailure(message: "このサーバーは API 音声認識の設定に対応していません。アプリを更新してください。")
        }
        if transcriptionModel.mode == .selected {
            guard let selection = transcriptionModel.selection,
                supportedTranscriptionProviders.contains(selection.provider) else {
                throw ConfigurationFormFailure(message: "API 音声認識に対応する接続とモデルを選んでください。")
            }
            guard effectiveExternalSendPolicy == "api_ok" else {
                throw ConfigurationFormFailure(message: "API 音声認識には外部送信ポリシー api_ok の明示設定が必要です。")
            }
            guard TranscriptionModelForm.isSupportedLanguage(effectiveLanguage) else {
                throw ConfigurationFormFailure(message: "API 音声認識の言語には auto または小文字2文字の ISO 639-1 コードを入力してください。")
            }
        }
        let trimmedName = selfName.trimmingCharacters(in: .whitespaces)
        return ProcessingConfigurationUpdate(config: MeetingConfig(
            transcriptionEngine: transcriptionEngine.isEmpty ? nil : transcriptionEngine,
            diarizationEngine: diarizationEngine.isEmpty ? nil : diarizationEngine,
            llmProvider: llmProvider.isEmpty ? nil : llmProvider,
            externalSendPolicy: externalSendPolicy.isEmpty ? nil : externalSendPolicy,
            language: language.isEmpty ? nil : language,
            selfName: trimmedName.isEmpty ? nil : trimmedName, vocabHints: vocabHints,
            minutesModel: minutesGenerationMode == .single ? minutesModel.selection : nil,
            minutesEnsemble: minutesGenerationMode == .ensemble ? minutesEnsemble.selection : nil,
            transcriptionModel: transcriptionModel.mode == .selected ? transcriptionModel.selection : nil),
            includesMinutesModel: supportsMinutesModel, includesMinutesEnsemble: supportsMinutesEnsembleWire,
            includesTranscriptionModel: supportsTranscriptionModel)
    }

    private static func policyAllows(participants: [MinutesModelSelection], policy: String) -> Bool {
        participants.allSatisfy { selection in
            switch selection.provider {
            case "openai-api", "anthropic-api": return policy == "api_ok"
            case "codex-app-server": return policy == "subscription_ok" || policy == "api_ok"
            case "ollama": return ["local_only", "subscription_ok", "api_ok"].contains(policy)
            default: return false
            }
        }
    }
}

public struct ProcessingConfigurationUpdate: Encodable, Equatable, Sendable {
    public let config: MeetingConfig
    let includesMinutesModel: Bool
    let includesMinutesEnsemble: Bool
    let includesTranscriptionModel: Bool
    enum ExplicitKeys: String, CodingKey {
        case selfName = "self_name"
        case minutesModel = "minutes_model"
        case minutesEnsemble = "minutes_ensemble"
        case transcriptionModel = "transcription_model"
    }

    public func encode(to encoder: Encoder) throws {
        try config.encode(to: encoder)
        var container = encoder.container(keyedBy: ExplicitKeys.self)
        try container.encode(config.selfName, forKey: .selfName)
        // Omission would retain a previous override instead of switching back to legacy.
        if includesMinutesModel { try container.encode(config.minutesModel, forKey: .minutesModel) }
        if includesMinutesEnsemble { try container.encode(config.minutesEnsemble, forKey: .minutesEnsemble) }
        if includesTranscriptionModel { try container.encode(config.transcriptionModel, forKey: .transcriptionModel) }
    }

    /// Resolve omitted fields exactly as a sparse set_meeting_config / set_profile update does.
    public func applying(to original: MeetingConfig) -> MeetingConfig {
        var result = original
        if let value = config.transcriptionEngine { result.transcriptionEngine = value }
        if let value = config.diarizationEngine { result.diarizationEngine = value }
        if let value = config.llmProvider { result.llmProvider = value }
        if let value = config.externalSendPolicy { result.externalSendPolicy = value }
        if let value = config.language { result.language = value }
        result.selfName = config.selfName
        result.vocabHints = config.vocabHints
        if includesMinutesModel { result.minutesModel = config.minutesModel }
        if includesMinutesEnsemble { result.minutesEnsemble = config.minutesEnsemble }
        if includesTranscriptionModel { result.transcriptionModel = config.transcriptionModel }
        return result
    }
}

public struct ConfigurationFormFailure: Error, LocalizedError, Equatable, Sendable {
    public let message: String
    public var errorDescription: String? { message }
    public init(message: String) { self.message = message }
}

public struct MeetingConfigurationForm: Equatable, Sendable {
    public var processing: ProcessingConfigurationForm
    public var scopeText: String
    public let meetingID: String?
    public let originalScope: String?

    public init() {
        processing = ProcessingConfigurationForm()
        scopeText = ""
        meetingID = nil
        originalScope = nil
    }

    public init(detail: MeetingDetail) {
        processing = ProcessingConfigurationForm(config: detail.config)
        scopeText = detail.meeting.scope ?? ""
        meetingID = detail.meeting.meetingID
        originalScope = detail.meeting.scope
    }
}

public struct ProfileConfigurationForm: Equatable, Sendable {
    public var name = ""
    public var processing = ProcessingConfigurationForm()
    public var scope = ""
    public var engagement = ""
    public var exportDestinations: Set<String> = []
    public var makeDefault = false
    public var isNew = true

    public init() {}

    public var expectedConfig: MeetingConfig { isNew ? .serverDefaults : processing.originalConfig }

    public init(profile: Profile) {
        name = profile.name
        processing = ProcessingConfigurationForm(config: profile.config)
        scope = profile.scope ?? ""
        engagement = profile.engagement ?? ""
        exportDestinations = Set(profile.exportDestinations)
        makeDefault = profile.isDefault
        isNew = false
    }
}
