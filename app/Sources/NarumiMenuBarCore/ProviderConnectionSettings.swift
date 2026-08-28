import Foundation

/// Editable connection fields. A credential is never populated from a response and is
/// removed from this state as soon as a save request is handed to the client.
public struct ProviderConnectionSettings: Sendable {
    public static let anthropicEndpoint = "https://api.anthropic.com"
    public static let openaiEndpoint = "https://api.openai.com"
    public static let ollamaEndpoint = "http://127.0.0.1:11434"
    public static let codexEndpoint = "https://chatgpt.com"

    public private(set) var connection: ProviderConnection?
    public private(set) var providerID: ProviderID
    public var displayName: String
    public var enabled: Bool
    public var endpoint: String
    public var apiKey = ""
    public private(set) var clearAPIKey = false

    public init(providerID: ProviderID = .anthropicAPI) {
        self.providerID = providerID
        displayName = ""
        enabled = true
        endpoint = Self.defaultEndpoint(for: providerID)
    }

    public init(connection: ProviderConnection) {
        self.connection = connection
        providerID = connection.providerID
        displayName = connection.displayName
        enabled = connection.enabled
        endpoint = connection.endpoint ?? Self.defaultEndpoint(for: connection.providerID)
    }

    public var isCreating: Bool { connection == nil }
    public var usesAPIKey: Bool { providerID.supportedAuthMethod == .apiKey }
    public var normalizedName: String { displayName.trimmingCharacters(in: .whitespacesAndNewlines) }
    public var normalizedEndpoint: String {
        providerID == .ollama
            ? endpoint.trimmingCharacters(in: .whitespacesAndNewlines) : Self.defaultEndpoint(for: providerID)
    }

    public var hasUnsavedChanges: Bool {
        guard let connection else {
            return !normalizedName.isEmpty || !apiKey.isEmpty || clearAPIKey
        }
        return normalizedName != connection.displayName || enabled != connection.enabled
            || normalizedEndpoint != connection.endpoint || !apiKey.isEmpty || clearAPIKey
    }

    public var canSave: Bool {
        !normalizedName.isEmpty && normalizedName.count <= 160 && apiKey.count <= 4096
            && isEndpointValid && hasUnsavedChanges
    }

    public var isEndpointValid: Bool {
        if providerID != .ollama { return true }
        let pattern = #"\Ahttps?://(127(\.(25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])){3}|\[::1\])(:[0-9]{1,5})?/?\z"#
        guard normalizedEndpoint.range(of: pattern, options: .regularExpression) != nil,
            let url = URLComponents(string: normalizedEndpoint) else { return false }
        if let port = url.port { return (1...65535).contains(port) }
        return true
    }

    public mutating func selectProvider(_ providerID: ProviderID) {
        guard isCreating, self.providerID != providerID else { return }
        self.providerID = providerID
        endpoint = Self.defaultEndpoint(for: providerID)
        clearSensitiveInput()
    }

    public mutating func setClearAPIKey(_ clear: Bool) {
        clearAPIKey = clear && usesAPIKey && !isCreating
        if clearAPIKey { apiKey = "" }
    }

    public mutating func clearSensitiveInput() {
        apiKey = ""
        clearAPIKey = false
    }

    public mutating func takeSaveRequest(requestID: String = UUID().uuidString) -> SetProviderConnectionRequest? {
        guard canSave else { return nil }
        let credential: ProviderCredentialUpdate
        if !usesAPIKey {
            credential = .unchanged
        } else if clearAPIKey {
            credential = .clear
        } else if apiKey.isEmpty {
            credential = .unchanged
        } else {
            credential = .replace(apiKey)
        }
        // Neither success, failure, closing the sheet nor a delayed response can restore it.
        apiKey = ""
        if let connection {
            return SetProviderConnectionRequest(
                connectionID: connection.connectionID, expectedRevision: connection.revision,
                displayName: normalizedName, enabled: enabled, endpoint: normalizedEndpoint,
                apiKey: credential, requestID: requestID)
        }
        return SetProviderConnectionRequest(
            providerID: providerID, displayName: normalizedName,
            authMethod: providerID.supportedAuthMethod, enabled: enabled,
            endpoint: normalizedEndpoint, apiKey: credential, requestID: requestID)
    }

    public mutating func adopt(_ connection: ProviderConnection) {
        self = Self(connection: connection)
    }

    public mutating func synchronizeStatus(_ connection: ProviderConnection) {
        guard self.connection?.connectionID == connection.connectionID,
            self.connection?.revision == connection.revision else { return }
        self.connection = connection
    }

    public mutating func dismiss() {
        if let connection { self = Self(connection: connection) }
        else { self = Self(providerID: providerID) }
    }

    private static func defaultEndpoint(for providerID: ProviderID) -> String {
        switch providerID {
        case .anthropicAPI, .claudeAgentSDK: return anthropicEndpoint
        case .openaiAPI: return openaiEndpoint
        case .ollama: return ollamaEndpoint
        case .codexAppServer: return codexEndpoint
        }
    }
}
