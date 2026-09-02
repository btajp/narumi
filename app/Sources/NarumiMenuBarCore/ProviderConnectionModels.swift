import Foundation

public enum ProviderAuthState: String, Codable, Sendable {
    case unconfigured, unverified, authenticating, authenticated, failed, unknown
}

public enum ProviderCatalogState: String, Codable, Sendable {
    case unfetched, ready, stale, failed
    case authenticationRequired = "authentication_required"
}

public enum ProviderAuthOperationState: String, Codable, Sendable {
    case pending, succeeded, failed, cancelled, unknown
}

public enum ProviderAuthAction: String, Codable, Sendable {
    case start, cancel, logout
}

public enum ProviderGenerationState: String, Codable, Sendable {
    case never, succeeded, failed, cancelled, unknown
}

public enum ProviderAPISurface: String, Codable, CaseIterable, Sendable {
    case responses
    case chatCompletions = "chat_completions"
}

public enum ProviderChatMaxTokensField: String, Codable, CaseIterable, Sendable {
    case maxTokens = "max_tokens"
    case maxCompletionTokens = "max_completion_tokens"
}

public struct ProviderActiveAuth: Decodable, Equatable, Sendable {
    public let operationID: String
    public let startRequestID: String
    public let serverInstanceID: String
    public let state: ProviderAuthOperationState

    enum CodingKeys: String, CodingKey {
        case operationID = "operation_id"
        case startRequestID = "start_request_id"
        case serverInstanceID = "server_instance_id"
        case state
    }

    public init(
        operationID: String, startRequestID: String, serverInstanceID: String,
        state: ProviderAuthOperationState
    ) {
        self.operationID = operationID
        self.startRequestID = startRequestID
        self.serverInstanceID = serverInstanceID
        self.state = state
    }
}

/// Only credential presence is returned; keys and SecretStore identifiers are not response fields.
public struct ProviderConnection: Decodable, Equatable, Sendable {
    public let connectionID: String
    public let revision: Int
    public let providerID: ProviderID
    public let displayName: String
    public let enabled: Bool
    public let endpoint: String?
    public let authMethod: ProviderAuthMethod
    public let apiSurface: ProviderAPISurface?
    public let chatMaxTokensField: ProviderChatMaxTokensField?
    public let credentialPresent: Bool
    public let authState: ProviderAuthState
    public let catalogState: ProviderCatalogState
    public let checkedAt: String?
    public let activeAuth: ProviderActiveAuth?
    public let lastGenerationState: ProviderGenerationState

    enum CodingKeys: String, CodingKey {
        case connectionID = "connection_id"
        case revision
        case providerID = "provider_id"
        case displayName = "display_name"
        case enabled, endpoint
        case authMethod = "auth_method"
        case apiSurface = "api_surface"
        case chatMaxTokensField = "chat_max_tokens_field"
        case credentialPresent = "credential_present"
        case authState = "auth_state"
        case catalogState = "catalog_state"
        case checkedAt = "checked_at"
        case activeAuth = "active_auth"
        case lastGenerationState = "last_generation_state"
    }

    public init(
        connectionID: String, revision: Int, providerID: ProviderID, displayName: String,
        enabled: Bool, endpoint: String?, authMethod: ProviderAuthMethod,
        apiSurface: ProviderAPISurface? = nil, chatMaxTokensField: ProviderChatMaxTokensField? = nil,
        credentialPresent: Bool, authState: ProviderAuthState, catalogState: ProviderCatalogState,
        checkedAt: String?, activeAuth: ProviderActiveAuth?, lastGenerationState: ProviderGenerationState
    ) {
        self.connectionID = connectionID
        self.revision = revision
        self.providerID = providerID
        self.displayName = displayName
        self.enabled = enabled
        self.endpoint = endpoint
        self.authMethod = authMethod
        self.apiSurface = apiSurface
        self.chatMaxTokensField = chatMaxTokensField
        self.credentialPresent = credentialPresent
        self.authState = authState
        self.catalogState = catalogState
        self.checkedAt = checkedAt
        self.activeAuth = activeAuth
        self.lastGenerationState = lastGenerationState
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        connectionID = try container.decode(String.self, forKey: .connectionID)
        revision = try container.decode(Int.self, forKey: .revision)
        providerID = try container.decode(ProviderID.self, forKey: .providerID)
        displayName = try container.decode(String.self, forKey: .displayName)
        enabled = try container.decode(Bool.self, forKey: .enabled)
        endpoint = try container.decode(String?.self, forKey: .endpoint)
        authMethod = try container.decode(ProviderAuthMethod.self, forKey: .authMethod)
        apiSurface = try container.decodeIfPresent(ProviderAPISurface.self, forKey: .apiSurface)
        chatMaxTokensField = try container.decodeIfPresent(ProviderChatMaxTokensField.self, forKey: .chatMaxTokensField)
        credentialPresent = try container.decode(Bool.self, forKey: .credentialPresent)
        authState = try container.decode(ProviderAuthState.self, forKey: .authState)
        catalogState = try container.decode(ProviderCatalogState.self, forKey: .catalogState)
        checkedAt = try container.decode(String?.self, forKey: .checkedAt)
        activeAuth = try container.decode(ProviderActiveAuth?.self, forKey: .activeAuth)
        lastGenerationState = try container.decode(ProviderGenerationState.self, forKey: .lastGenerationState)
        guard revision > 0, providerID.supportedAuthMethods.contains(authMethod),
            providerID != .ollama || !credentialPresent,
            providerID != .openaiAPI || endpoint == ProviderConnectionSettings.openaiEndpoint,
            providerID != .codexAppServer || endpoint == ProviderConnectionSettings.codexEndpoint,
            providerID != .openaiAPI || apiSurface == nil || apiSurface == .responses,
            [.openaiAPI, .openAICompatibleAPI].contains(providerID) || apiSurface == nil,
            providerID == .openAICompatibleAPI || chatMaxTokensField == nil,
            providerID != .openAICompatibleAPI || ProviderConnectionSettings.isCompatibleEndpointValid(
                endpoint ?? "", authMethod: authMethod),
            providerID != .openAICompatibleAPI || apiSurface != nil,
            providerID != .openAICompatibleAPI || authMethod != .none || !credentialPresent,
            apiSurface != .chatCompletions || chatMaxTokensField != nil,
            apiSurface == .chatCompletions || chatMaxTokensField == nil
        else {
            throw DecodingError.dataCorruptedError(
                forKey: .authMethod, in: container,
                debugDescription: "Connection revision or authentication metadata violates the contract")
        }
    }
}

public struct ProviderAuthOperation: Decodable, Equatable, Sendable,
    CustomStringConvertible, CustomDebugStringConvertible, CustomReflectable
{
    public let operationID: String
    public let connectionID: String
    public let connectionRevision: Int
    public let serverInstanceID: String
    public let startRequestID: String
    public let action: ProviderAuthAction
    public let state: ProviderAuthOperationState
    public let authorizationURL: ProviderAuthorizationURL?
    public let userCode: ProviderUserCode?
    public let reason: String?
    public let createdAt: String
    public let updatedAt: String

    enum CodingKeys: String, CodingKey {
        case operationID = "operation_id"
        case connectionID = "connection_id"
        case connectionRevision = "connection_revision"
        case serverInstanceID = "server_instance_id"
        case startRequestID = "start_request_id"
        case action, state
        case authorizationURL = "authorization_url"
        case userCode = "user_code"
        case reason
        case createdAt = "created_at"
        case updatedAt = "updated_at"
    }

    public init(
        operationID: String, connectionID: String, connectionRevision: Int,
        serverInstanceID: String, startRequestID: String, action: ProviderAuthAction,
        state: ProviderAuthOperationState, authorizationURL: ProviderAuthorizationURL? = nil,
        userCode: ProviderUserCode? = nil, reason: String?,
        createdAt: String, updatedAt: String
    ) {
        self.operationID = operationID
        self.connectionID = connectionID
        self.connectionRevision = connectionRevision
        self.serverInstanceID = serverInstanceID
        self.startRequestID = startRequestID
        self.action = action
        self.state = state
        self.authorizationURL = authorizationURL
        self.userCode = userCode
        self.reason = reason
        self.createdAt = createdAt
        self.updatedAt = updatedAt
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        operationID = try container.decode(String.self, forKey: .operationID)
        connectionID = try container.decode(String.self, forKey: .connectionID)
        connectionRevision = try container.decode(Int.self, forKey: .connectionRevision)
        serverInstanceID = try container.decode(String.self, forKey: .serverInstanceID)
        startRequestID = try container.decode(String.self, forKey: .startRequestID)
        action = try container.decode(ProviderAuthAction.self, forKey: .action)
        state = try container.decode(ProviderAuthOperationState.self, forKey: .state)
        authorizationURL = try container.decode(ProviderAuthorizationURL?.self, forKey: .authorizationURL)
        userCode = try container.decode(ProviderUserCode?.self, forKey: .userCode)
        guard connectionRevision > 0, (authorizationURL == nil) == (userCode == nil),
            authorizationURL == nil || (action == .start && state == .pending) else {
            throw DecodingError.dataCorruptedError(
                forKey: .authorizationURL, in: container,
                debugDescription: "Device authorization requires a URL and user code for a pending start operation")
        }
        reason = try container.decode(String?.self, forKey: .reason)
        createdAt = try container.decode(String.self, forKey: .createdAt)
        updatedAt = try container.decode(String.self, forKey: .updatedAt)
    }

    public var description: String { "ProviderAuthOperation(<redacted>)" }
    public var debugDescription: String { description }
    public var customMirror: Mirror {
        Mirror(self, children: ["operationID": operationID, "state": state.rawValue, "authorization": "<redacted>"])
    }
}
