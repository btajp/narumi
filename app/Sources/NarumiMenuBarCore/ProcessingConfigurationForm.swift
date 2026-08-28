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
    private let originalPolicy: String

    public init(config: MeetingConfig = MeetingConfig()) {
        transcriptionEngine = config.transcriptionEngine ?? ""
        diarizationEngine = config.diarizationEngine ?? ""
        llmProvider = config.llmProvider ?? ""
        externalSendPolicy = config.externalSendPolicy ?? ""
        language = config.language ?? ""
        selfName = config.selfName ?? ""
        vocabHintsText = (config.vocabHints ?? []).joined(separator: "\n")
        minutesModel = MinutesModelForm(selection: config.minutesModel)
        originalPolicy = config.externalSendPolicy ?? "local_only"
    }

    public var effectiveExternalSendPolicy: String {
        externalSendPolicy.isEmpty ? originalPolicy : externalSendPolicy
    }

    public var vocabHints: [String] {
        vocabHintsText.split(whereSeparator: \.isNewline)
            .map { $0.trimmingCharacters(in: .whitespaces) }.filter { !$0.isEmpty }
    }

    public func makeUpdate(supportsMinutesModel: Bool = true) throws -> ProcessingConfigurationUpdate {
        guard supportsMinutesModel || minutesModel.mode == .legacy else {
            throw ConfigurationFormFailure(message: "このサーバーは Codex の議事録モデル選択に対応していません。アプリを更新してください。")
        }
        guard minutesModel.mode != .codex || minutesModel.selection != nil else {
            throw ConfigurationFormFailure(message: "Codex の接続とモデルを選んでください。")
        }
        let trimmedName = selfName.trimmingCharacters(in: .whitespaces)
        return ProcessingConfigurationUpdate(config: MeetingConfig(
            transcriptionEngine: transcriptionEngine.isEmpty ? nil : transcriptionEngine,
            diarizationEngine: diarizationEngine.isEmpty ? nil : diarizationEngine,
            llmProvider: llmProvider.isEmpty ? nil : llmProvider,
            externalSendPolicy: externalSendPolicy.isEmpty ? nil : externalSendPolicy,
            language: language.isEmpty ? nil : language,
            selfName: trimmedName.isEmpty ? nil : trimmedName, vocabHints: vocabHints,
            minutesModel: minutesModel.mode == .codex ? minutesModel.selection : nil),
            includesMinutesModel: supportsMinutesModel)
    }
}

public struct ProcessingConfigurationUpdate: Encodable, Equatable, Sendable {
    public let config: MeetingConfig
    let includesMinutesModel: Bool
    enum ExplicitKeys: String, CodingKey {
        case selfName = "self_name"
        case minutesModel = "minutes_model"
    }

    public func encode(to encoder: Encoder) throws {
        try config.encode(to: encoder)
        var container = encoder.container(keyedBy: ExplicitKeys.self)
        try container.encode(config.selfName, forKey: .selfName)
        // Omission would retain a previous override instead of switching back to legacy.
        if includesMinutesModel { try container.encode(config.minutesModel, forKey: .minutesModel) }
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

    public init() {
        processing = ProcessingConfigurationForm()
        scopeText = ""
    }

    public init(detail: MeetingDetail) {
        processing = ProcessingConfigurationForm(config: detail.config)
        scopeText = detail.meeting.scope ?? ""
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
