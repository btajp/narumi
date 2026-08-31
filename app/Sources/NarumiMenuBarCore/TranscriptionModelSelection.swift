import Foundation

/// A pinned audio-only override. Its epoch alone never authorizes an unknown chunk retry.
public struct TranscriptionModelSelection: Codable, Equatable, Sendable {
    public static let providers = ["openai-api"]
    public static let modelIDs = ["whisper-1", "gpt-4o-transcribe-diarize"]
    public let provider: String
    public var connectionID: String
    public var connectionRevision: Int
    public var modelID: String
    public var parameters: Parameters
    public var cacheEpoch: Int

    /// The audio adapter owns all wire options; no user-defined options are accepted.
    public struct Parameters: Codable, Equatable, Sendable {
        public init() {}

        public init(from decoder: Decoder) throws {
            let container = try decoder.container(keyedBy: TranscriptionSelectionKey.self)
            guard container.allKeys.isEmpty else {
                throw DecodingError.dataCorrupted(.init(
                    codingPath: decoder.codingPath, debugDescription: "Transcription parameters must be empty"))
            }
        }

        public func encode(to encoder: Encoder) throws {
            _ = encoder.container(keyedBy: TranscriptionSelectionKey.self)
        }
    }

    enum CodingKeys: String, CodingKey {
        case provider
        case connectionID = "connection_id"
        case connectionRevision = "connection_revision"
        case modelID = "model_id"
        case parameters
        case cacheEpoch = "cache_epoch"
    }

    public init(
        provider: String = "openai-api", connectionID: String, connectionRevision: Int,
        modelID: String, parameters: Parameters = Parameters(), cacheEpoch: Int = 0
    ) {
        self.provider = provider
        self.connectionID = connectionID
        self.connectionRevision = connectionRevision
        self.modelID = modelID
        self.parameters = parameters
        self.cacheEpoch = cacheEpoch
    }

    public init(from decoder: Decoder) throws {
        let names = try decoder.container(keyedBy: TranscriptionSelectionKey.self)
        let allowed: Set<String> = ["provider", "connection_id", "connection_revision", "model_id", "parameters", "cache_epoch"]
        guard names.allKeys.allSatisfy({ allowed.contains($0.stringValue) }) else {
            throw DecodingError.dataCorrupted(.init(
                codingPath: decoder.codingPath, debugDescription: "Unsupported transcription selection field"))
        }
        let container = try decoder.container(keyedBy: CodingKeys.self)
        provider = try container.decode(String.self, forKey: .provider)
        connectionID = try container.decode(String.self, forKey: .connectionID)
        connectionRevision = try container.decode(Int.self, forKey: .connectionRevision)
        modelID = try container.decode(String.self, forKey: .modelID)
        parameters = container.contains(.parameters)
            ? try container.decode(Parameters.self, forKey: .parameters) : Parameters()
        cacheEpoch = container.contains(.cacheEpoch) ? try container.decode(Int.self, forKey: .cacheEpoch) : 0
        guard isWellFormed else {
            throw DecodingError.dataCorruptedError(
                forKey: .provider, in: container, debugDescription: "Invalid audio transcription selection")
        }
    }

    public func encode(to encoder: Encoder) throws {
        guard isWellFormed else {
            throw EncodingError.invalidValue("transcription selection", .init(
                codingPath: encoder.codingPath, debugDescription: "Invalid audio transcription selection"))
        }
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(provider, forKey: .provider)
        try container.encode(connectionID, forKey: .connectionID)
        try container.encode(connectionRevision, forKey: .connectionRevision)
        try container.encode(modelID, forKey: .modelID)
        try container.encode(parameters, forKey: .parameters)
        try container.encode(cacheEpoch, forKey: .cacheEpoch)
    }

    public var isWellFormed: Bool {
        Self.providers.contains(provider) && Self.modelIDs.contains(modelID)
            && connectionRevision > 0 && cacheEpoch >= 0
            && connectionID.range(of: #"\Aconn-[0-9a-f]{12,32}\z"#, options: .regularExpression) != nil
    }
}

private struct TranscriptionSelectionKey: CodingKey {
    let stringValue: String
    var intValue: Int? { nil }
    init?(stringValue: String) { self.stringValue = stringValue }
    init?(intValue: Int) { return nil }
}
