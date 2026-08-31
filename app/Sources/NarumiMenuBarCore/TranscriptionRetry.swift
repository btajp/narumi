import Foundation

/// Only locally verified plan/ledger facts can authorize review of an unknown ASR chunk.
public struct TranscriptionOutcomeUnknownDetails: Codable, Equatable, Sendable {
    public enum Track: String, Codable, Sendable { case mic, system }

    public let stage: String
    public let reason: String
    public let outcomeUnknown: Bool
    public let inputFingerprint: String
    public let chunkFingerprint: String
    public let blockedEpoch: Int
    public let track: Track
    public let chunkIndex: Int
    public let chunkCount: Int
    public let completedChunks: Int
    public let startSample: Int
    public let endSample: Int
    public let sampleRate: Int
    public let provider: String?
    public let modelID: String?
    public let connectionID: String?
    public let connectionRevision: Int?

    enum CodingKeys: String, CodingKey, CaseIterable {
        case stage, reason, track, provider
        case outcomeUnknown = "outcome_unknown"
        case inputFingerprint = "input_fingerprint"
        case chunkFingerprint = "chunk_fingerprint"
        case blockedEpoch = "blocked_epoch"
        case chunkIndex = "chunk_index"
        case chunkCount = "chunk_count"
        case completedChunks = "completed_chunks"
        case startSample = "start_sample"
        case endSample = "end_sample"
        case sampleRate = "sample_rate"
        case modelID = "model_id"
        case connectionID = "connection_id"
        case connectionRevision = "connection_revision"
    }

    public init(
        inputFingerprint: String, chunkFingerprint: String, blockedEpoch: Int,
        track: Track, chunkIndex: Int, chunkCount: Int, completedChunks: Int,
        startSample: Int, endSample: Int, sampleRate: Int = 16_000,
        provider: String? = nil, modelID: String? = nil,
        connectionID: String? = nil, connectionRevision: Int? = nil
    ) throws {
        guard validTranscriptionFingerprint(inputFingerprint), validTranscriptionFingerprint(chunkFingerprint),
            blockedEpoch >= 0, (1...144).contains(chunkCount), (0..<chunkCount).contains(chunkIndex),
            (0...chunkCount).contains(completedChunks), (0...1_382_399_999).contains(startSample),
            (1...1_382_400_000).contains(endSample), startSample < endSample,
            endSample - startSample <= 9_600_000, sampleRate == 16_000,
            provider.map({ $0 == "openai-api" }) ?? true,
            modelID.map({ ["whisper-1", "gpt-4o-transcribe-diarize"].contains($0) }) ?? true,
            connectionID.map({ $0.range(of: #"\Aconn-[0-9a-f]{12,32}\z"#, options: .regularExpression) != nil }) ?? true,
            connectionRevision.map({ $0 > 0 }) ?? true else {
            throw TranscriptionRetryValidationError.invalidEvidence
        }
        stage = "transcribe"
        reason = "transcription_outcome_unknown"
        outcomeUnknown = true
        self.inputFingerprint = inputFingerprint
        self.chunkFingerprint = chunkFingerprint
        self.blockedEpoch = blockedEpoch
        self.track = track
        self.chunkIndex = chunkIndex
        self.chunkCount = chunkCount
        self.completedChunks = completedChunks
        self.startSample = startSample
        self.endSample = endSample
        self.sampleRate = sampleRate
        self.provider = provider
        self.modelID = modelID
        self.connectionID = connectionID
        self.connectionRevision = connectionRevision
    }

    public init(from decoder: Decoder) throws {
        do {
            try requireTranscriptionKeys(decoder, allowed: Set(CodingKeys.allCases.map(\.rawValue)))
            let container = try decoder.container(keyedBy: CodingKeys.self)
            guard try container.decode(String.self, forKey: .stage) == "transcribe",
                try container.decode(String.self, forKey: .reason) == "transcription_outcome_unknown",
                try container.decode(Bool.self, forKey: .outcomeUnknown) else {
                throw TranscriptionRetryValidationError.invalidEvidence
            }
            try self.init(
                inputFingerprint: container.decode(String.self, forKey: .inputFingerprint),
                chunkFingerprint: container.decode(String.self, forKey: .chunkFingerprint),
                blockedEpoch: container.decode(Int.self, forKey: .blockedEpoch),
                track: container.decode(Track.self, forKey: .track),
                chunkIndex: container.decode(Int.self, forKey: .chunkIndex),
                chunkCount: container.decode(Int.self, forKey: .chunkCount),
                completedChunks: container.decode(Int.self, forKey: .completedChunks),
                startSample: container.decode(Int.self, forKey: .startSample),
                endSample: container.decode(Int.self, forKey: .endSample),
                sampleRate: container.decode(Int.self, forKey: .sampleRate),
                provider: container.contains(.provider) ? container.decode(String.self, forKey: .provider) : nil,
                modelID: container.contains(.modelID) ? container.decode(String.self, forKey: .modelID) : nil,
                connectionID: container.contains(.connectionID) ? container.decode(String.self, forKey: .connectionID) : nil,
                connectionRevision: container.contains(.connectionRevision)
                    ? container.decode(Int.self, forKey: .connectionRevision) : nil)
        } catch {
            throw DecodingError.dataCorrupted(.init(
                codingPath: decoder.codingPath, debugDescription: "Invalid transcription outcome evidence"))
        }
    }

    public var retry: TranscriptionRetry { TranscriptionRetry(details: self) }
    public var startSeconds: Double { Double(startSample) / Double(sampleRate) }
    public var endSeconds: Double { Double(endSample) / Double(sampleRate) }
}

/// A single explicit retry request. This is never part of a saved meeting configuration.
public struct TranscriptionRetry: Codable, Equatable, Sendable {
    public let inputFingerprint: String
    public let chunkFingerprint: String
    public let blockedEpoch: Int

    enum CodingKeys: String, CodingKey, CaseIterable {
        case inputFingerprint = "input_fingerprint"
        case chunkFingerprint = "chunk_fingerprint"
        case blockedEpoch = "blocked_epoch"
    }

    public init(inputFingerprint: String, chunkFingerprint: String, blockedEpoch: Int) throws {
        guard validTranscriptionFingerprint(inputFingerprint), validTranscriptionFingerprint(chunkFingerprint),
            blockedEpoch >= 0 else { throw TranscriptionRetryValidationError.invalidRetry }
        self.inputFingerprint = inputFingerprint
        self.chunkFingerprint = chunkFingerprint
        self.blockedEpoch = blockedEpoch
    }

    public init(details: TranscriptionOutcomeUnknownDetails) {
        inputFingerprint = details.inputFingerprint
        chunkFingerprint = details.chunkFingerprint
        blockedEpoch = details.blockedEpoch
    }

    public var isWellFormed: Bool {
        validTranscriptionFingerprint(inputFingerprint) && validTranscriptionFingerprint(chunkFingerprint) && blockedEpoch >= 0
    }

    public init(from decoder: Decoder) throws {
        do {
            try requireTranscriptionKeys(decoder, allowed: Set(CodingKeys.allCases.map(\.rawValue)))
            let container = try decoder.container(keyedBy: CodingKeys.self)
            try self.init(
                inputFingerprint: container.decode(String.self, forKey: .inputFingerprint),
                chunkFingerprint: container.decode(String.self, forKey: .chunkFingerprint),
                blockedEpoch: container.decode(Int.self, forKey: .blockedEpoch))
        } catch {
            throw DecodingError.dataCorrupted(.init(
                codingPath: decoder.codingPath, debugDescription: "Invalid transcription retry confirmation"))
        }
    }
}

public enum TranscriptionRetryValidationError: Error, Equatable, LocalizedError {
    case invalidEvidence, invalidRetry

    public var errorDescription: String? {
        "音声認識の再試行に必要な確認情報が不正です。ジョブの状態を再取得してください。"
    }
}

private func validTranscriptionFingerprint(_ value: String) -> Bool {
    value.utf8.count == 64 && value.utf8.allSatisfy { (48...57).contains($0) || (97...102).contains($0) }
}

private func requireTranscriptionKeys(_ decoder: Decoder, allowed: Set<String>) throws {
    let container = try decoder.container(keyedBy: TranscriptionRetryCodingKey.self)
    guard container.allKeys.allSatisfy({ allowed.contains($0.stringValue) }) else {
        throw TranscriptionRetryValidationError.invalidEvidence
    }
}

private struct TranscriptionRetryCodingKey: CodingKey {
    let stringValue: String
    var intValue: Int? { nil }
    init?(stringValue: String) { self.stringValue = stringValue }
    init?(intValue: Int) { return nil }
}
