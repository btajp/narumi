import Foundation

public protocol MCPHTTPTransporting: Sendable {
    func data(for request: URLRequest, protectingSecrets: Bool) async throws -> (Data, URLResponse)
    func invalidate()
}

/// Every resident MCP call uses the same authenticated TLS boundary, including discovery.
/// Sessions are scoped to immutable bootstrap material; reconnecting creates a new transport.
public final class MCPHTTPTransport: MCPHTTPTransporting {
    public static let confidentialErrorMessage = "接続設定を更新できませんでした。接続状態と入力内容を確認してください。"

    private let connection: MCPServerConnection
    private let serverTrust: MCPPinnedServerTrust
    private let ordinarySession: URLSession
    private let permissionSession: URLSession
    private let confidentialSession: URLSession

    public init(connection: MCPServerConnection) {
        self.connection = connection
        let delegate = MCPPinnedServerTrust(bootstrap: connection.bootstrap)
        serverTrust = delegate
        ordinarySession = URLSession(configuration: Self.ordinaryConfiguration(), delegate: delegate, delegateQueue: nil)
        permissionSession = URLSession(configuration: Self.permissionConfiguration(), delegate: delegate, delegateQueue: nil)
        confidentialSession = URLSession(configuration: Self.confidentialConfiguration(), delegate: delegate, delegateQueue: nil)
    }

    deinit { invalidate() }

    public func invalidate() {
        ordinarySession.invalidateAndCancel()
        permissionSession.invalidateAndCancel()
        confidentialSession.invalidateAndCancel()
    }

    public static func isConfidentialTool(_ name: String) -> Bool {
        [ToolCatalog.setGaiaConnection, ToolCatalog.setProviderConnection,
            ToolCatalog.authenticateProviderConnection, ToolCatalog.deleteProviderConnection].contains(name)
    }

    public func data(for request: URLRequest, protectingSecrets: Bool = false) async throws -> (Data, URLResponse) {
        let plan = try Self.requestPlan(for: request, protectingSecrets: protectingSecrets)
        guard plan.request.url == connection.bootstrap.url else { throw MCPConnectionError.endpointMismatch }
        var prepared = plan.request
        // URLSession transmits this only after the pinned server-trust challenge succeeds.
        prepared.setValue(connection.authorization, forHTTPHeaderField: "Authorization")
        do {
            switch plan.route {
            case .ordinary: return try await ordinarySession.data(for: prepared)
            case .permissionSetup: return try await permissionSession.data(for: prepared)
            case .confidential: return try await confidentialSession.data(for: prepared)
            }
        } catch is CancellationError {
            throw CancellationError()
        } catch {
            // URLSession's errors can contain URLs and reflected peer content.
            if serverTrust.rejectedPeer { throw MCPConnectionError.certificateMismatch }
            if Task.isCancelled { throw CancellationError() }
            throw MCPConnectionError.transportFailed
        }
    }

    enum RequestRoute: Equatable { case ordinary, permissionSetup, confidential }

    static func requestPlan(
        for request: URLRequest, protectingSecrets: Bool = false
    ) throws -> (route: RequestRoute, request: URLRequest) {
        var prepared = request
        prepared.url = try MCPServerEndpoint.validate(request.url)
        guard request.httpBodyStream == nil,
            ["POST", "GET", "DELETE"].contains(request.httpMethod ?? "GET")
        else { throw MCPConnectionError.invalidEndpoint }
        // An injected Host or Origin would otherwise escape the exact endpoint binding.
        prepared.setValue(nil, forHTTPHeaderField: "Host")
        prepared.setValue(nil, forHTTPHeaderField: "Origin")
        prepared.setValue(nil, forHTTPHeaderField: "Proxy-Authorization")
        prepared.setValue(nil, forHTTPHeaderField: "Cookie")
        prepared.httpShouldHandleCookies = false
        prepared.cachePolicy = .reloadIgnoringLocalCacheData
        let route: RequestRoute
        if protectingSecrets || hasConfidentialBody(request) {
            route = .confidential
        } else if calledTool(request) == ToolCatalog.configureRecordingPermission {
            prepared.timeoutInterval = 150
            route = .permissionSetup
        } else {
            route = .ordinary
        }
        return (route, prepared)
    }

    public static func confidentialEndpoint(_ url: URL?) throws -> URL {
        try MCPServerEndpoint.validate(url)
    }

    public static func confidentialErrorCode(_ untrustedCode: String?) -> String {
        let allowed: Set<String> = [
            "not_found", "scope_denied", "contract_mismatch", "busy", "cancelled",
            "invalid_argument", "policy_violation", "recorder_unavailable", "engine_unavailable", "internal",
            "authentication_required", "configuration_conflict", "model_unavailable",
        ]
        guard let untrustedCode, allowed.contains(untrustedCode) else { return "internal" }
        return untrustedCode
    }

    static func hasConfidentialBody(_ request: URLRequest) -> Bool {
        guard let name = calledTool(request) else { return false }
        return isConfidentialTool(name)
    }

    private static func calledTool(_ request: URLRequest) -> String? {
        guard let body = request.httpBody,
            let rpc = try? JSONSerialization.jsonObject(with: body) as? [String: Any],
            rpc["method"] as? String == "tools/call",
            let params = rpc["params"] as? [String: Any],
            let name = params["name"] as? String
        else { return nil }
        return name
    }

    static func ordinaryConfiguration() -> URLSessionConfiguration {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.timeoutIntervalForRequest = 30
        configuration.timeoutIntervalForResource = 120
        configuration.tlsMinimumSupportedProtocolVersion = .TLSv12
        configuration.connectionProxyDictionary = [
            "HTTPEnable": 0, "HTTPSEnable": 0, "SOCKSEnable": 0,
            "ProxyAutoConfigEnable": 0, "ProxyAutoDiscoveryEnable": 0,
        ]
        configuration.httpCookieStorage = nil
        configuration.httpShouldSetCookies = false
        configuration.urlCredentialStorage = nil
        configuration.urlCache = nil
        configuration.requestCachePolicy = .reloadIgnoringLocalCacheData
        return configuration
    }

    static func permissionConfiguration() -> URLSessionConfiguration {
        let configuration = ordinaryConfiguration()
        configuration.timeoutIntervalForRequest = 150
        configuration.timeoutIntervalForResource = 180
        return configuration
    }

    static func confidentialConfiguration() -> URLSessionConfiguration { ordinaryConfiguration() }
}
