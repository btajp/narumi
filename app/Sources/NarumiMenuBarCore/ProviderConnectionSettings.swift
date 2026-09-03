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
    public var endpoint: String {
        didSet {
            guard providerID == .openAICompatibleAPI, endpoint != oldValue, !apiKey.isEmpty else { return }
            // A key typed for one user-controlled host must never be carried to another host.
            // The user must enter it again after the destination is final.
            apiKey = ""
            clearAPIKey = false
            endpointChangeClearedAPIKey = true
        }
    }
    public var authMethod: ProviderAuthMethod
    public var apiSurface: ProviderAPISurface
    public var chatMaxTokensField: ProviderChatMaxTokensField
    public var apiKey = "" {
        didSet {
            if !apiKey.isEmpty { endpointChangeClearedAPIKey = false }
        }
    }
    public private(set) var clearAPIKey = false
    private var endpointChangeClearedAPIKey = false

    public init(providerID: ProviderID = .anthropicAPI) {
        self.providerID = providerID
        displayName = ""
        enabled = true
        endpoint = Self.defaultEndpoint(for: providerID)
        authMethod = providerID.defaultAuthMethod
        apiSurface = .responses
        chatMaxTokensField = .maxTokens
    }

    public init(connection: ProviderConnection) {
        self.connection = connection
        providerID = connection.providerID
        displayName = connection.displayName
        enabled = connection.enabled
        endpoint = connection.endpoint ?? Self.defaultEndpoint(for: connection.providerID)
        authMethod = connection.authMethod
        apiSurface = connection.apiSurface ?? .responses
        chatMaxTokensField = connection.chatMaxTokensField ?? .maxTokens
    }

    public var isCreating: Bool { connection == nil }
    public var usesAPIKey: Bool { authMethod == .apiKey }
    public var normalizedName: String { displayName.trimmingCharacters(in: .whitespacesAndNewlines) }
    public var normalizedEndpoint: String {
        [ProviderID.ollama, .openAICompatibleAPI].contains(providerID)
            ? endpoint.trimmingCharacters(in: .whitespacesAndNewlines) : Self.defaultEndpoint(for: providerID)
    }

    public var hasUnsavedChanges: Bool {
        guard let connection else {
            return !normalizedName.isEmpty || enabled != true
                || normalizedEndpoint != Self.defaultEndpoint(for: providerID)
                || authMethod != providerID.defaultAuthMethod
                || (providerID == .openAICompatibleAPI && (
                    apiSurface != .responses || chatMaxTokensField != .maxTokens))
                || !apiKey.isEmpty || clearAPIKey
        }
        return normalizedName != connection.displayName || enabled != connection.enabled
            || normalizedEndpoint != connection.endpoint || authMethod != connection.authMethod
            || apiSurface != (connection.apiSurface ?? .responses)
            || (apiSurface == .chatCompletions && chatMaxTokensField != connection.chatMaxTokensField)
            || !apiKey.isEmpty || clearAPIKey
    }

    public var canSave: Bool {
        !normalizedName.isEmpty && normalizedName.count <= 160 && apiKey.count <= 4096
            && isEndpointValid && !requiresAPIKeyReentryForEndpointChange && hasUnsavedChanges
    }

    public var requiresAPIKeyReentryForEndpointChange: Bool {
        if endpointChangeClearedAPIKey && usesAPIKey { return true }
        guard let connection, providerID == .openAICompatibleAPI, authMethod == .apiKey,
            connection.credentialPresent, normalizedEndpoint != connection.endpoint else { return false }
        return apiKey.isEmpty && !clearAPIKey
    }

    public var isEndpointValid: Bool {
        if providerID == .openAICompatibleAPI {
            return Self.isCompatibleEndpointValid(normalizedEndpoint, authMethod: authMethod)
        }
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
        authMethod = providerID.defaultAuthMethod
        apiSurface = .responses
        chatMaxTokensField = .maxTokens
        clearSensitiveInput()
    }

    public mutating func selectAuthMethod(_ method: ProviderAuthMethod) {
        guard providerID.supportedAuthMethods.contains(method), authMethod != method else { return }
        authMethod = method
        clearSensitiveInput()
    }

    public mutating func setClearAPIKey(_ clear: Bool) {
        clearAPIKey = clear && usesAPIKey && !isCreating
        if clearAPIKey {
            apiKey = ""
            endpointChangeClearedAPIKey = false
        }
    }

    public mutating func clearSensitiveInput() {
        apiKey = ""
        clearAPIKey = false
        endpointChangeClearedAPIKey = false
    }

    public mutating func takeSaveRequest(requestID: String = UUID().uuidString) -> SetProviderConnectionRequest? {
        guard canSave else { return nil }
        let credential: ProviderCredentialUpdate
        if authMethod == .none, let connection, connection.authMethod != .none {
            credential = .clear
        } else if !usesAPIKey {
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
                authMethod: providerID == .openAICompatibleAPI ? authMethod : nil,
                apiSurface: providerID == .openAICompatibleAPI ? apiSurface : nil,
                chatMaxTokensField: providerID == .openAICompatibleAPI && apiSurface == .chatCompletions
                    ? chatMaxTokensField : nil,
                clearChatMaxTokensField: providerID == .openAICompatibleAPI && apiSurface == .responses,
                apiKey: credential, requestID: requestID)
        }
        return SetProviderConnectionRequest(
            providerID: providerID, displayName: normalizedName,
            authMethod: authMethod, enabled: enabled, endpoint: normalizedEndpoint,
            apiSurface: providerID == .openAICompatibleAPI ? apiSurface : nil,
            chatMaxTokensField: providerID == .openAICompatibleAPI && apiSurface == .chatCompletions
                ? chatMaxTokensField : nil,
            apiKey: credential, requestID: requestID)
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
        case .openAICompatibleAPI: return ""
        case .ollama: return ollamaEndpoint
        case .codexAppServer: return codexEndpoint
        }
    }

    public static func isCompatibleEndpointValid(_ value: String, authMethod: ProviderAuthMethod) -> Bool {
        guard value == value.trimmingCharacters(in: .whitespacesAndNewlines), !value.isEmpty,
            value.count <= 2048, value.last != "/", !value.contains("\\"), !value.contains("%"),
            value.unicodeScalars.allSatisfy({ $0.value >= 0x20 && $0.value != 0x7f }),
            let components = URLComponents(string: value),
            components.user == nil, components.password == nil,
            components.query == nil, components.fragment == nil,
            let scheme = components.scheme, let host = components.host,
            !host.isEmpty, components.string == value
        else { return false }
        guard isCanonicalCompatibleAuthority(value, components: components, host: host),
            isCompatiblePath(components.path) else { return false }
        let loopback = isNumericLoopback(host)
        guard scheme == "https" || (scheme == "http" && loopback) else { return false }
        return loopback || (authMethod == .apiKey && isRemoteDNSName(host))
    }

    private static func isNumericLoopback(_ host: String) -> Bool {
        if host == "::1" || host == "[::1]" { return true }
        let octets = host.split(separator: ".", omittingEmptySubsequences: false)
        guard octets.count == 4, octets[0] == "127" else { return false }
        return octets.allSatisfy { part in
            !part.isEmpty && part.utf8.allSatisfy({ (48...57).contains($0) })
                && Int(part).map { (0...255).contains($0) && String($0) == part } == true
        }
    }

    private static func isRemoteDNSName(_ host: String) -> Bool {
        let labels = host.split(separator: ".", omittingEmptySubsequences: false)
        guard labels.count >= 2,
            host.utf8.contains(where: { (65...90).contains($0) || (97...122).contains($0) }) else { return false }
        return labels.allSatisfy { label in
            guard (1...63).contains(label.utf8.count), let first = label.utf8.first,
                let last = label.utf8.last, isASCIIAlphaNumeric(first), isASCIIAlphaNumeric(last) else { return false }
            return label.utf8.allSatisfy { isASCIIAlphaNumeric($0) || $0 == 45 }
        }
    }

    private static func isCompatiblePath(_ path: String) -> Bool {
        guard path.isEmpty || path.first == "/" else { return false }
        return path.split(separator: "/", omittingEmptySubsequences: false).dropFirst().allSatisfy { segment in
            guard (1...128).contains(segment.utf8.count), let first = segment.utf8.first,
                isASCIIAlphaNumeric(first) else { return false }
            return segment.utf8.allSatisfy {
                isASCIIAlphaNumeric($0) || [45, 46, 95, 126].contains($0)
            }
        }
    }

    private static func isCanonicalCompatibleAuthority(
        _ value: String, components: URLComponents, host: String
    ) -> Bool {
        guard let schemeRange = value.range(of: "://") else { return false }
        let remainder = value[schemeRange.upperBound...]
        let authority = String(remainder.prefix { $0 != "/" })
        let hostLiteral = host == "::1" || host == "[::1]" ? "[::1]" : host
        if authority == hostLiteral { return components.port == nil }
        guard authority.hasPrefix(hostLiteral + ":"), let port = components.port,
            (1...65535).contains(port) else { return false }
        let rawPort = authority.dropFirst(hostLiteral.count + 1)
        return !rawPort.isEmpty && rawPort.first != "0" && rawPort.count <= 5
            && rawPort.utf8.allSatisfy { (48...57).contains($0) } && Int(rawPort) == port
    }

    private static func isASCIIAlphaNumeric(_ byte: UInt8) -> Bool {
        (48...57).contains(byte) || (65...90).contains(byte) || (97...122).contains(byte)
    }
}
