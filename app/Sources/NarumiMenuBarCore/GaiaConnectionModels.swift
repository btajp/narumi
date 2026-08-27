import Foundation

/// Public, credential-free settings returned by get/set_gaia_connection.
public struct GaiaConnection: Codable, Equatable, Sendable {
    public enum Source: String, Codable, Sendable {
        case saved
        case environment
        case unconfigured

        public var label: String {
            switch self {
            case .saved: return "保存済み（環境変数より優先）"
            case .environment: return "環境変数（アプリからは未保存）"
            case .unconfigured: return "未設定"
            }
        }
    }

    public let url: String?
    public let hasAPIKey: Bool
    public let source: Source

    enum CodingKeys: String, CodingKey {
        case url
        case hasAPIKey = "has_api_key"
        case source
    }

    public init(url: String?, hasAPIKey: Bool, source: Source) {
        self.url = url
        self.hasAPIKey = hasAPIKey
        self.source = source
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        // Nullable is not optional: the contract requires the key even when disabled.
        url = try container.decode(String?.self, forKey: .url)
        hasAPIKey = try container.decode(Bool.self, forKey: .hasAPIKey)
        source = try container.decode(Source.self, forKey: .source)
    }

    public func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(url, forKey: .url)
        try container.encode(hasAPIKey, forKey: .hasAPIKey)
        try container.encode(source, forKey: .source)
    }
}

public struct GaiaConnectionResponse: Decodable, Sendable {
    public let connection: GaiaConnection
}

/// Write-only input: omission retains a key for the same URL; JSON null explicitly clears it.
/// Deliberately not Decodable: no server response can populate an API key input.
public struct SetGaiaConnectionRequest: Encodable, Sendable {
    public enum APIKeyUpdate: Sendable {
        case unchanged
        case clear
        case replace(String)
    }

    public let url: String?
    public let apiKey: APIKeyUpdate
    public let requestID: String

    enum CodingKeys: String, CodingKey {
        case url
        case apiKey = "api_key"
        case requestID = "request_id"
    }

    public init(
        url: String?, apiKey: APIKeyUpdate = .unchanged,
        requestID: String = UUID().uuidString
    ) {
        self.url = url
        self.apiKey = apiKey
        self.requestID = requestID
    }

    public func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        if let url {
            try container.encode(url, forKey: .url)
        } else {
            // An omitted URL would not disable Gaia.
            try container.encodeNil(forKey: .url)
        }
        switch apiKey {
        case .unchanged: break
        case .clear: try container.encodeNil(forKey: .apiKey)
        case .replace(let key): try container.encode(key, forKey: .apiKey)
        }
        try container.encode(requestID, forKey: .requestID)
    }
}

public struct TestGaiaConnectionRequest: Encodable, Sendable {
    public let timeoutSeconds: Double

    enum CodingKeys: String, CodingKey {
        case timeoutSeconds = "timeout_seconds"
    }

    public init(timeoutSeconds: Double = 5) {
        self.timeoutSeconds = timeoutSeconds
    }
}

public struct GaiaConnectionTestResult: Decodable, Equatable, Sendable {
    public struct ClientIdentity: Decodable, Equatable, Sendable {
        public enum Role: String, Decodable, Sendable {
            case agent
            case human
        }

        public let name: String
        public let role: Role
        public let defaultScope: String?

        enum CodingKeys: String, CodingKey {
            case name
            case role
            case defaultScope = "default_scope"
        }

        public init(from decoder: Decoder) throws {
            let container = try decoder.container(keyedBy: CodingKeys.self)
            name = try container.decode(String.self, forKey: .name)
            role = try container.decode(Role.self, forKey: .role)
            defaultScope = try container.decode(String?.self, forKey: .defaultScope)
        }
    }

    public let connected: Bool
    public let name: String
    public let version: String
    public let contractVersion: String
    public let client: ClientIdentity

    enum CodingKeys: String, CodingKey {
        case connected
        case name
        case version
        case contractVersion = "contract_version"
        case client
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        connected = try container.decode(Bool.self, forKey: .connected)
        guard connected else {
            throw DecodingError.dataCorruptedError(
                forKey: .connected, in: container,
                debugDescription: "test_gaia_connection must confirm a successful connection")
        }
        name = try container.decode(String.self, forKey: .name)
        version = try container.decode(String.self, forKey: .version)
        contractVersion = try container.decode(String.self, forKey: .contractVersion)
        client = try container.decode(ClientIdentity.self, forKey: .client)
    }
}
