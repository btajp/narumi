import Foundation

/// Only public fields survive an ambiguous save. Never retain the write-only credential
/// or resend a create with a new request ID merely because its response was lost.
struct ProviderSettingsSaveRecovery: Equatable, Sendable {
    enum CredentialIntent: Equatable, Sendable { case unchanged, clear, reenter }

    let requestID: String
    let connectionID: String?
    let expectedRevision: Int?
    let providerID: ProviderID
    let displayName: String
    let enabled: Bool
    let endpoint: String
    let previousConnectionIDs: Set<String>
    let credentialIntent: CredentialIntent
    let requestProviderID: ProviderID?
    let requestDisplayName: String?
    let requestEnabled: Bool?
    let requestEndpoint: String?
    let requestAuthMethod: ProviderAuthMethod?
    let requestAPISurface: ProviderAPISurface?
    let requestChatMaxTokensField: ProviderChatMaxTokensField?
    let requestClearsChatMaxTokensField: Bool
    private(set) var receiptConnectionID: String?

    init(editor: ProviderConnectionSettings, connections: [ProviderConnection], request: SetProviderConnectionRequest) {
        requestID = request.requestID
        connectionID = editor.connection?.connectionID
        expectedRevision = editor.connection?.revision
        providerID = editor.providerID
        displayName = editor.normalizedName
        enabled = editor.enabled
        endpoint = Self.origin(editor.normalizedEndpoint)
        previousConnectionIDs = Set(connections.map(\.connectionID))
        switch request.apiKey {
        case .unchanged:
            credentialIntent = .unchanged
        case .clear:
            credentialIntent = .clear
        case .replace:
            credentialIntent = .reenter
        }
        requestProviderID = request.providerID
        requestDisplayName = request.displayName
        requestEnabled = request.enabled
        requestEndpoint = request.endpoint
        requestAuthMethod = request.authMethod
        requestAPISurface = request.apiSurface
        requestChatMaxTokensField = request.chatMaxTokensField
        requestClearsChatMaxTokensField = request.clearsChatMaxTokensField
    }

    func confirmedConnection(in connections: [ProviderConnection]) -> ProviderConnection? {
        // Matching public fields cannot prove which request wrote a connection,
        // including whether its write-only credential matches the original input.
        guard let receiptConnectionID else { return nil }
        return connections.first { $0.connectionID == receiptConnectionID }
    }

    func canAdoptAfterReview(_ connection: ProviderConnection) -> Bool {
        guard connection.providerID == providerID else { return false }
        if let target = receiptConnectionID ?? connectionID { return connection.connectionID == target }
        return !previousConnectionIDs.contains(connection.connectionID)
    }

    mutating func confirmReceipt(_ connection: ProviderConnection) -> Bool {
        guard connection.providerID == providerID,
            connectionID == nil || connectionID == connection.connectionID else { return false }
        receiptConnectionID = connection.connectionID
        return true
    }

    func retryRequest(apiKey: String?) -> SetProviderConnectionRequest? {
        let credential: ProviderCredentialUpdate
        switch credentialIntent {
        case .unchanged: credential = .unchanged
        case .clear: credential = .clear
        case .reenter:
            guard let apiKey, !apiKey.isEmpty else { return nil }
            credential = .replace(apiKey)
        }
        if let connectionID, let expectedRevision {
            return SetProviderConnectionRequest(
                connectionID: connectionID, expectedRevision: expectedRevision,
                displayName: requestDisplayName, enabled: requestEnabled, endpoint: requestEndpoint,
                authMethod: requestAuthMethod, apiSurface: requestAPISurface,
                chatMaxTokensField: requestChatMaxTokensField,
                clearChatMaxTokensField: requestClearsChatMaxTokensField,
                apiKey: credential, requestID: requestID)
        }
        guard let requestProviderID, let requestDisplayName, let requestAuthMethod else { return nil }
        return SetProviderConnectionRequest(
            providerID: requestProviderID, displayName: requestDisplayName, authMethod: requestAuthMethod,
            enabled: requestEnabled ?? true, endpoint: requestEndpoint, apiSurface: requestAPISurface,
            chatMaxTokensField: requestChatMaxTokensField,
            apiKey: credential, requestID: requestID)
    }

    private static func origin(_ endpoint: String) -> String {
        endpoint.hasSuffix("/") ? String(endpoint.dropLast()) : endpoint
    }
}

/// Safe review fields for an unresolved save. Credential intent is represented by a Boolean,
/// never a retained value, digest or Keychain identifier.
public struct ProviderSaveRecoverySummary: Equatable, Sendable {
    public let requestID: String
    public let connectionID: String?
    public let providerID: ProviderID
    public let displayName: String
    public let endpoint: String
    public let enabled: Bool
    public let requiresAPIKeyReentry: Bool
    public let receiptConfirmed: Bool
}
