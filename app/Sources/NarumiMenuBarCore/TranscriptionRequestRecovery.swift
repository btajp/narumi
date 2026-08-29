import Foundation

/// An immutable copy of one desktop request whose acceptance was not confirmed.
/// Recovery must use these exact bytes and request ID, never rebuild its arguments.
public struct TranscriptionRequestRecovery: Equatable, Sendable, Identifiable {
    public let requestID: String
    public let meetingID: String
    public let scope: String?
    public let expectedConfig: MeetingConfig
    public let transcriptionRetry: TranscriptionRetry
    public let arguments: Data

    public var id: String { requestID }

    public init(request: DesktopJobRequestState.Request) throws {
        do {
            guard request.tool == ToolCatalog.regenerate else {
                throw TranscriptionRequestRecoveryError.invalidRequest
            }
            var scanner = RecoveryJSONKeyScanner(data: request.arguments)
            try scanner.requireUniqueKeys()
            let decoded = try JSONDecoder().decode(RecoveryArguments.self, from: request.arguments)
            guard decoded.requestID.utf8.elementsEqual(request.requestID.utf8) else {
                throw TranscriptionRequestRecoveryError.invalidRequest
            }
            requestID = request.requestID
            meetingID = decoded.meetingID
            scope = decoded.scope
            expectedConfig = decoded.expectedConfig
            transcriptionRetry = decoded.transcriptionRetry
            arguments = request.arguments
        } catch {
            // Neither decoder errors nor the original request may reach UI error text.
            throw TranscriptionRequestRecoveryError.invalidRequest
        }
    }
}

public enum TranscriptionRequestRecoveryError: Error, Equatable, LocalizedError, Sendable {
    case invalidRequest

    public var errorDescription: String? {
        "音声認識の受付確認に必要な保存済みリクエストが不正です。元の処理状態を確認してください。"
    }
}

private struct RecoveryArguments: Decodable {
    let requestID: String
    let meetingID: String
    let scope: String?
    let expectedConfig: MeetingConfig
    let transcriptionRetry: TranscriptionRetry

    enum CodingKeys: String, CodingKey, CaseIterable {
        case requestID = "request_id"
        case meetingID = "meeting_id"
        case expectedConfig = "expected_config"
        case transcriptionRetry = "transcription_retry"
        case scope, force, reason
    }

    init(from decoder: Decoder) throws {
        try requireRecoveryKeys(decoder, allowed: Set(CodingKeys.allCases.map(\.rawValue)))
        let container = try decoder.container(keyedBy: CodingKeys.self)
        requestID = try container.decode(String.self, forKey: .requestID)
        meetingID = try container.decode(String.self, forKey: .meetingID)
        // Desktop requests carry at most one scope. Cross-scope API requests are not
        // converted into a narrower selector when recovering from this UI.
        scope = container.contains(.scope) ? try container.decode(String.self, forKey: .scope) : nil
        expectedConfig = try container.decode(RecoveryConfiguration.self, forKey: .expectedConfig).value
        transcriptionRetry = try container.decode(TranscriptionRetry.self, forKey: .transcriptionRetry)
        guard (8...128).contains(requestID.unicodeScalars.count),
            meetingID.range(of: #"\A[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}\z"#, options: .regularExpression) != nil,
            scope.map({ (1...64).contains($0.unicodeScalars.count) }) ?? true,
            let selection = expectedConfig.transcriptionModel, selection.isWellFormed,
            transcriptionRetry.isWellFormed, selection.cacheEpoch > transcriptionRetry.blockedEpoch else {
            throw TranscriptionRequestRecoveryError.invalidRequest
        }
        if container.contains(.force), try container.decode(Bool.self, forKey: .force) {
            throw TranscriptionRequestRecoveryError.invalidRequest
        }
        if container.contains(.reason) {
            let reason = try container.decode(String.self, forKey: .reason)
            guard (1...500).contains(reason.unicodeScalars.count) else {
                throw TranscriptionRequestRecoveryError.invalidRequest
            }
        }
    }
}

private struct RecoveryConfiguration: Decodable {
    let value: MeetingConfig

    init(from decoder: Decoder) throws {
        try requireRecoveryKeys(decoder, allowed: [
            "transcription_engine", "diarization_engine", "llm_provider", "minutes_model", "minutes_ensemble",
            "transcription_model",
            "external_send_policy", "language", "self_name", "vocab_hints",
        ])
        let container = try decoder.container(keyedBy: MeetingConfig.CodingKeys.self)
        // A recovery record needs the full effective config; sparse updates cannot
        // stand in for the settings the user confirmed before the original send.
        value = try MeetingConfig(
            transcriptionEngine: container.decode(String.self, forKey: .transcriptionEngine),
            diarizationEngine: container.decode(String.self, forKey: .diarizationEngine),
            llmProvider: container.decode(String.self, forKey: .llmProvider),
            externalSendPolicy: container.decode(String.self, forKey: .externalSendPolicy),
            language: container.decode(String.self, forKey: .language),
            selfName: container.decodeIfPresent(String.self, forKey: .selfName),
            vocabHints: container.decode([String].self, forKey: .vocabHints),
            minutesModel: container.decodeIfPresent(MinutesModelSelection.self, forKey: .minutesModel),
            minutesEnsemble: container.decodeIfPresent(MinutesEnsembleSelection.self, forKey: .minutesEnsemble),
            transcriptionModel: container.decode(TranscriptionModelSelection.self, forKey: .transcriptionModel))
        guard value.externalSendPolicy == "api_ok",
            value.language.map(TranscriptionModelForm.isSupportedLanguage) == true,
            value.minutesModel.map({ $0.isWellFormed && $0.modelID.unicodeScalars.count <= 256 }) ?? true,
            value.minutesEnsemble.map(\.isWellFormed) ?? true,
            value.minutesModel == nil || value.minutesEnsemble == nil else {
            throw TranscriptionRequestRecoveryError.invalidRequest
        }
    }
}

private func requireRecoveryKeys(_ decoder: Decoder, allowed: Set<String>) throws {
    let container = try decoder.container(keyedBy: RecoveryField.self)
    guard container.allKeys.allSatisfy({ allowed.contains($0.stringValue) }) else {
        throw TranscriptionRequestRecoveryError.invalidRequest
    }
}

private struct RecoveryField: CodingKey {
    let stringValue: String
    var intValue: Int? { nil }
    init?(stringValue: String) { self.stringValue = stringValue }
    init?(intValue: Int) { return nil }
}

/// JSONDecoder validates all values and syntax. This scanner only adds duplicate
/// key rejection before a decoder could select a different occurrence of a key.
private struct RecoveryJSONKeyScanner {
    let bytes: [UInt8]
    var cursor = 0

    init(data: Data) { bytes = Array(data) }

    mutating func requireUniqueKeys() throws {
        try value(depth: 0)
        skipWhitespace()
        guard cursor == bytes.count else { throw TranscriptionRequestRecoveryError.invalidRequest }
    }

    private mutating func value(depth: Int) throws {
        guard depth < 64 else { throw TranscriptionRequestRecoveryError.invalidRequest }
        skipWhitespace()
        if peek(123) {
            try object(depth: depth + 1)
        } else if peek(91) {
            try array(depth: depth + 1)
        } else if peek(34) {
            _ = try string()
        } else {
            let start = cursor
            while cursor < bytes.count && ![9, 10, 13, 32, 44, 93, 125].contains(bytes[cursor]) {
                cursor += 1
            }
            guard cursor > start else { throw TranscriptionRequestRecoveryError.invalidRequest }
        }
    }

    private mutating func object(depth: Int) throws {
        try consume(123)
        var keys: Set<String> = []
        skipWhitespace()
        if !peek(125) {
            while true {
                let key = try string()
                guard keys.insert(key).inserted else { throw TranscriptionRequestRecoveryError.invalidRequest }
                try consume(58)
                try value(depth: depth)
                skipWhitespace()
                if peek(125) { break }
                try consume(44)
            }
        }
        try consume(125)
    }

    private mutating func array(depth: Int) throws {
        try consume(91)
        skipWhitespace()
        if !peek(93) {
            while true {
                try value(depth: depth)
                skipWhitespace()
                if peek(93) { break }
                try consume(44)
            }
        }
        try consume(93)
    }

    private mutating func string() throws -> String {
        skipWhitespace()
        let start = cursor
        try consume(34)
        while cursor < bytes.count {
            if peek(34) {
                cursor += 1
                return try JSONDecoder().decode(String.self, from: Data(bytes[start..<cursor]))
            }
            if peek(92) {
                cursor += 1
                guard cursor < bytes.count else { throw TranscriptionRequestRecoveryError.invalidRequest }
            }
            cursor += 1
        }
        throw TranscriptionRequestRecoveryError.invalidRequest
    }

    private func peek(_ byte: UInt8) -> Bool { cursor < bytes.count && bytes[cursor] == byte }

    private mutating func consume(_ byte: UInt8) throws {
        skipWhitespace()
        guard peek(byte) else { throw TranscriptionRequestRecoveryError.invalidRequest }
        cursor += 1
    }

    private mutating func skipWhitespace() {
        while cursor < bytes.count && [9, 10, 13, 32].contains(bytes[cursor]) { cursor += 1 }
    }
}
