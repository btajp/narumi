import Foundation

/// A pinned, text-only minutes override. Other stages continue to use their existing config.
public struct CodexMinutesSelection: Codable, Equatable, Sendable {
    public let provider: String
    public var connectionID: String
    public var connectionRevision: Int
    public var modelID: String
    public var parameters: Parameters
    public var cacheEpoch: Int

    public struct Parameters: Codable, Equatable, Sendable {
        public var reasoningEffort: String?

        enum CodingKeys: String, CodingKey { case reasoningEffort = "reasoning_effort" }

        public init(reasoningEffort: String? = nil) { self.reasoningEffort = reasoningEffort }

        public init(from decoder: Decoder) throws {
            let names = try decoder.container(keyedBy: SelectionKey.self)
            guard names.allKeys.allSatisfy({ $0.stringValue == "reasoning_effort" }) else {
                throw DecodingError.dataCorrupted(.init(
                    codingPath: decoder.codingPath, debugDescription: "Unsupported minutes model parameter"))
            }
            let container = try decoder.container(keyedBy: CodingKeys.self)
            reasoningEffort = container.contains(.reasoningEffort)
                ? try container.decode(String.self, forKey: .reasoningEffort) : nil
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
        connectionID: String, connectionRevision: Int, modelID: String,
        reasoningEffort: String? = nil, cacheEpoch: Int = 0
    ) {
        provider = "codex-app-server"
        self.connectionID = connectionID
        self.connectionRevision = connectionRevision
        self.modelID = modelID
        parameters = Parameters(reasoningEffort: reasoningEffort)
        self.cacheEpoch = cacheEpoch
    }

    public init(from decoder: Decoder) throws {
        let names = try decoder.container(keyedBy: SelectionKey.self)
        let allowed: Set<String> = ["provider", "connection_id", "connection_revision", "model_id", "parameters", "cache_epoch"]
        guard names.allKeys.allSatisfy({ allowed.contains($0.stringValue) }) else {
            throw DecodingError.dataCorrupted(.init(
                codingPath: decoder.codingPath, debugDescription: "Unsupported minutes model selection field"))
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
                forKey: .provider, in: container, debugDescription: "Invalid text minutes selection")
        }
    }

    public func encode(to encoder: Encoder) throws {
        guard isWellFormed else {
            throw EncodingError.invalidValue("minutes model selection", .init(
                codingPath: encoder.codingPath, debugDescription: "Invalid text minutes selection"))
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
        provider == "codex-app-server" && connectionRevision > 0 && cacheEpoch >= 0
            && connectionID.range(of: #"\Aconn-[a-f0-9]{12,32}\z"#, options: .regularExpression) != nil
            && !modelID.isEmpty && modelID.count <= 256
            && (parameters.reasoningEffort.map {
                $0.range(of: #"\A[a-z][a-z0-9_-]{0,31}\z"#, options: .regularExpression) != nil
            } ?? true)
    }
}

private struct SelectionKey: CodingKey {
    let stringValue: String
    var intValue: Int? { nil }
    init?(stringValue: String) { self.stringValue = stringValue }
    init?(intValue: Int) { return nil }
}
