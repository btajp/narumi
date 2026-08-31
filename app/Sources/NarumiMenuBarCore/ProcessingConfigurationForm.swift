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
    public var minutesModel: MinutesModelForm
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
        minutesModel = MinutesModelForm(selection: config.minutesModel)
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

    public func makeUpdate(
        supportsMinutesModel: Bool = true, supportedProviders: [String] = MinutesModelSelection.providers,
        supportsTranscriptionModel: Bool = true,
        supportedTranscriptionProviders: [String] = TranscriptionModelSelection.providers
    ) throws -> ProcessingConfigurationUpdate {
        guard supportsMinutesModel || minutesModel.mode == .legacy else {
            throw ConfigurationFormFailure(message: "このサーバーは議事録モデル選択に対応していません。アプリを更新してください。")
        }
        guard minutesModel.mode != .selected || (minutesModel.selection != nil && supportedProviders.contains(minutesModel.provider)) else {
            throw ConfigurationFormFailure(message: "対応するプロバイダの接続・モデルと、利用できるパラメータを選んでください。")
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
            minutesModel: minutesModel.mode == .selected ? minutesModel.selection : nil,
            transcriptionModel: transcriptionModel.mode == .selected ? transcriptionModel.selection : nil),
            includesMinutesModel: supportsMinutesModel, includesTranscriptionModel: supportsTranscriptionModel)
    }
}

public struct ProcessingConfigurationUpdate: Encodable, Equatable, Sendable {
    public let config: MeetingConfig
    let includesMinutesModel: Bool
    let includesTranscriptionModel: Bool
    enum ExplicitKeys: String, CodingKey {
        case selfName = "self_name"
        case minutesModel = "minutes_model"
        case transcriptionModel = "transcription_model"
    }

    public func encode(to encoder: Encoder) throws {
        try config.encode(to: encoder)
        var container = encoder.container(keyedBy: ExplicitKeys.self)
        try container.encode(config.selfName, forKey: .selfName)
        // Omission would retain a previous override instead of switching back to legacy.
        if includesMinutesModel { try container.encode(config.minutesModel, forKey: .minutesModel) }
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
