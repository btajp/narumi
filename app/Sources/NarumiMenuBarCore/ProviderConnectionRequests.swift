import Foundation

/// Write-only mutation. Omission, deletion and replacement have distinct JSON representations.
public enum ProviderCredentialUpdate: Equatable, Sendable, CustomStringConvertible,
    CustomDebugStringConvertible, CustomReflectable
{
    case unchanged
    case clear
    case replace(String)

    public var description: String {
        switch self {
        case .unchanged: return "unchanged"
        case .clear: return "clear"
        case .replace: return "replace(<redacted>)"
        }
    }

    public var debugDescription: String { description }
    public var customMirror: Mirror { Mirror(self, children: ["state": description]) }
}

/// Deliberately Encodable-only: a response can never populate an API-key input.
public struct SetProviderConnectionRequest: Encodable, Equatable, Sendable,
    CustomStringConvertible, CustomDebugStringConvertible
{
    public let connectionID: String?
    public let expectedRevision: Int?
    public let providerID: ProviderID?
    public let displayName: String?
    public let enabled: Bool?
    public let endpoint: String?
    public let authMethod: ProviderAuthMethod?
    public let apiKey: ProviderCredentialUpdate
    public let requestID: String

    enum CodingKeys: String, CodingKey {
        case connectionID = "connection_id"
        case expectedRevision = "expected_revision"
        case providerID = "provider_id"
        case displayName = "display_name"
        case enabled, endpoint
        case authMethod = "auth_method"
        case apiKey = "api_key"
        case requestID = "request_id"
    }

    public init(
        providerID: ProviderID, displayName: String, authMethod: ProviderAuthMethod,
        enabled: Bool = true, endpoint: String? = nil, apiKey: ProviderCredentialUpdate = .unchanged,
        requestID: String = UUID().uuidString
    ) {
        connectionID = nil
        expectedRevision = nil
        self.providerID = providerID
        self.displayName = displayName
        self.enabled = enabled
        self.endpoint = endpoint
        self.authMethod = authMethod
        self.apiKey = apiKey
        self.requestID = requestID
    }

    public init(
        connectionID: String, expectedRevision: Int, displayName: String? = nil,
        enabled: Bool? = nil, endpoint: String? = nil, authMethod: ProviderAuthMethod? = nil,
        apiKey: ProviderCredentialUpdate = .unchanged, requestID: String = UUID().uuidString
    ) {
        self.connectionID = connectionID
        self.expectedRevision = expectedRevision
        providerID = nil
        self.displayName = displayName
        self.enabled = enabled
        self.endpoint = endpoint
        self.authMethod = authMethod
        self.apiKey = apiKey
        self.requestID = requestID
    }

    public var description: String { "SetProviderConnectionRequest(<redacted>)" }
    public var debugDescription: String { description }

    public func encode(to encoder: Encoder) throws {
        if connectionID != nil {
            guard let expectedRevision, expectedRevision > 0,
                displayName != nil || enabled != nil || endpoint != nil || authMethod != nil || apiKey != .unchanged
            else { throw invalidProviderRequest(encoder) }
        }
        if let providerID, authMethod != providerID.supportedAuthMethod {
            throw invalidProviderRequest(encoder)
        }
        if authMethod == .chatgpt || providerID == .codexAppServer {
            guard apiKey == .unchanged,
                endpoint == nil || endpoint == ProviderConnectionSettings.codexEndpoint else {
                throw invalidProviderRequest(encoder)
            }
        }
        if case .replace(let value) = apiKey {
            guard !value.isEmpty, value.count <= 4096,
                authMethod != ProviderAuthMethod.none, authMethod != .chatgpt else {
                throw invalidProviderRequest(encoder)
            }
        }
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encodeIfPresent(connectionID, forKey: .connectionID)
        try container.encodeIfPresent(expectedRevision, forKey: .expectedRevision)
        try container.encodeIfPresent(providerID, forKey: .providerID)
        try container.encodeIfPresent(displayName, forKey: .displayName)
        try container.encodeIfPresent(enabled, forKey: .enabled)
        try container.encodeIfPresent(endpoint, forKey: .endpoint)
        try container.encodeIfPresent(authMethod, forKey: .authMethod)
        switch apiKey {
        case .unchanged: break
        case .clear: try container.encodeNil(forKey: .apiKey)
        case .replace(let value): try container.encode(value, forKey: .apiKey)
        }
        try container.encode(requestID, forKey: .requestID)
    }
}

public struct DeleteProviderConnectionRequest: Encodable, Equatable, Sendable {
    public let connectionID: String
    public let expectedRevision: Int
    public let confirm: Bool
    public let requestID: String

    enum CodingKeys: String, CodingKey {
        case connectionID = "connection_id"
        case expectedRevision = "expected_revision"
        case confirm
        case requestID = "request_id"
    }

    public init(
        connectionID: String, expectedRevision: Int, confirm: Bool,
        requestID: String = UUID().uuidString
    ) {
        self.connectionID = connectionID
        self.expectedRevision = expectedRevision
        self.confirm = confirm
        self.requestID = requestID
    }

    public func encode(to encoder: Encoder) throws {
        guard confirm, expectedRevision > 0 else { throw invalidProviderRequest(encoder) }
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(connectionID, forKey: .connectionID)
        try container.encode(expectedRevision, forKey: .expectedRevision)
        try container.encode(confirm, forKey: .confirm)
        try container.encode(requestID, forKey: .requestID)
    }
}

public struct AuthenticateProviderConnectionRequest: Encodable, Equatable, Sendable {
    public let connectionID: String
    public let expectedRevision: Int
    public let action: ProviderAuthAction
    public let operationID: String?
    public let requestID: String

    enum CodingKeys: String, CodingKey {
        case connectionID = "connection_id"
        case expectedRevision = "expected_revision"
        case action
        case operationID = "operation_id"
        case requestID = "request_id"
    }

    public init(
        connectionID: String, expectedRevision: Int, action: ProviderAuthAction,
        operationID: String? = nil, requestID: String = UUID().uuidString
    ) {
        self.connectionID = connectionID
        self.expectedRevision = expectedRevision
        self.action = action
        self.operationID = operationID
        self.requestID = requestID
    }

    public func encode(to encoder: Encoder) throws {
        guard expectedRevision > 0, (action == .cancel) == (operationID != nil) else {
            throw invalidProviderRequest(encoder)
        }
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(connectionID, forKey: .connectionID)
        try container.encode(expectedRevision, forKey: .expectedRevision)
        try container.encode(action, forKey: .action)
        try container.encodeIfPresent(operationID, forKey: .operationID)
        try container.encode(requestID, forKey: .requestID)
    }
}

public struct GetProviderAuthStatusRequest: Encodable, Equatable, Sendable {
    public let connectionID: String
    public let operationID: String?
    public let startRequestID: String?

    enum CodingKeys: String, CodingKey {
        case connectionID = "connection_id"
        case operationID = "operation_id"
        case startRequestID = "start_request_id"
    }

    public init(connectionID: String, operationID: String) {
        self.connectionID = connectionID
        self.operationID = operationID
        startRequestID = nil
    }

    public init(connectionID: String, startRequestID: String) {
        self.connectionID = connectionID
        operationID = nil
        self.startRequestID = startRequestID
    }
}

public struct TestProviderConnectionRequest: Encodable, Equatable, Sendable {
    public let connectionID: String
    public let expectedRevision: Int

    enum CodingKeys: String, CodingKey {
        case connectionID = "connection_id"
        case expectedRevision = "expected_revision"
    }

    public init(connectionID: String, expectedRevision: Int) {
        self.connectionID = connectionID
        self.expectedRevision = expectedRevision
    }

    public func encode(to encoder: Encoder) throws {
        guard expectedRevision > 0 else { throw invalidProviderRequest(encoder) }
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(connectionID, forKey: .connectionID)
        try container.encode(expectedRevision, forKey: .expectedRevision)
    }
}

public struct ListProviderModelsRequest: Encodable, Equatable, Sendable {
    public let connectionID: String
    public let role: ProviderRole
    public let cursor: String?
    public let refresh: Bool

    enum CodingKeys: String, CodingKey {
        case connectionID = "connection_id"
        case role, cursor, refresh
    }

    public init(connectionID: String, role: ProviderRole = .llm, cursor: String? = nil, refresh: Bool = false) {
        self.connectionID = connectionID
        self.role = role
        self.cursor = cursor
        self.refresh = refresh
    }
}

public enum ProviderRuntimeAction: String, Codable, Sendable {
    case prepare, update
}

public struct PrepareProviderRuntimeRequest: Encodable, Equatable, Sendable {
    public let providerID: ProviderID
    public let resourceID: String
    public let expectedCatalogRevision: String
    public let action: ProviderRuntimeAction
    public let requestID: String

    enum CodingKeys: String, CodingKey {
        case providerID = "provider_id"
        case resourceID = "resource_id"
        case expectedCatalogRevision = "expected_catalog_revision"
        case action
        case requestID = "request_id"
    }

    public init(
        providerID: ProviderID, resourceID: String, expectedCatalogRevision: String,
        action: ProviderRuntimeAction = .prepare, requestID: String = UUID().uuidString
    ) {
        self.providerID = providerID
        self.resourceID = resourceID
        self.expectedCatalogRevision = expectedCatalogRevision
        self.action = action
        self.requestID = requestID
    }
}

private func invalidProviderRequest(_ encoder: Encoder) -> EncodingError {
    // Do not retain the request or a secret in an error's associated value.
    .invalidValue(
        "<redacted>",
        .init(codingPath: encoder.codingPath, debugDescription: "Provider request does not match its contract"))
}
