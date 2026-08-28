import Darwin
import Foundation

/// Keeps write-only tool inputs off remote endpoints, proxies, redirects and persistent stores.
/// Ordinary MCP calls retain the app's existing URLSession behavior.
public final class MCPHTTPTransport: Sendable {
    public static let confidentialErrorMessage = "Gaia 接続設定を保存できませんでした。接続先と入力内容を確認してください。"

    private let ordinarySession: URLSession
    private let permissionSession: URLSession
    private let confidentialSession: URLSession

    public init() {
        ordinarySession = URLSession(configuration: Self.ordinaryConfiguration())
        permissionSession = URLSession(configuration: Self.permissionConfiguration())
        confidentialSession = URLSession(
            configuration: Self.confidentialConfiguration(),
            delegate: RejectRedirects(), delegateQueue: nil)
    }

    deinit {
        ordinarySession.invalidateAndCancel()
        permissionSession.invalidateAndCancel()
        confidentialSession.invalidateAndCancel()
    }

    public static func isConfidentialTool(_ name: String) -> Bool {
        name == ToolCatalog.setGaiaConnection
    }

    /// The body check is mandatory even if a caller forgets to protect session initialization.
    public func data(for request: URLRequest, protectingSecrets: Bool = false) async throws -> (Data, URLResponse) {
        let plan = try Self.requestPlan(for: request, protectingSecrets: protectingSecrets)
        switch plan.route {
        case .ordinary: return try await ordinarySession.data(for: plan.request)
        case .permissionSetup: return try await permissionSession.data(for: plan.request)
        case .confidential: return try await confidentialSession.data(for: plan.request)
        }
    }

    enum RequestRoute: Equatable {
        case ordinary, permissionSetup, confidential
    }

    /// A pure send plan keeps the selected session and its per-request safeguards together.
    static func requestPlan(
        for request: URLRequest, protectingSecrets: Bool = false
    ) throws -> (route: RequestRoute, request: URLRequest) {
        var prepared = request
        if protectingSecrets || hasConfidentialBody(request) {
            prepared.url = try confidentialEndpoint(request.url)
            prepared.httpShouldHandleCookies = false
            prepared.cachePolicy = .reloadIgnoringLocalCacheData
            return (.confidential, prepared)
        }
        if calledTool(request) == ToolCatalog.configureRecordingPermission {
            // A caller's shorter URLRequest timeout otherwise overrides the session setting.
            prepared.timeoutInterval = 150
            return (.permissionSetup, prepared)
        }
        return (.ordinary, prepared)
    }

    /// Never resolve a hostname while sending a secret. Even localhost is pinned numerically.
    public static func confidentialEndpoint(_ url: URL?) throws -> URL {
        guard let url,
            var components = URLComponents(url: url, resolvingAgainstBaseURL: false),
            components.scheme?.lowercased() == "http",
            components.user == nil, components.password == nil,
            components.query == nil, components.fragment == nil,
            let host = components.host?.lowercased(),
            components.percentEncodedHost?.contains("%") != true,
            components.port.map({ (1...65535).contains($0) }) ?? true
        else {
            throw ConfidentialEndpointError()
        }

        if host == "localhost" {
            components.host = "127.0.0.1"
        } else if isIPv4Loopback(host) {
            components.host = host
        } else if isIPv6Loopback(host) {
            components.host = "[::1]"
        } else {
            throw ConfidentialEndpointError()
        }
        guard let validated = components.url else {
            throw ConfidentialEndpointError()
        }
        return validated
    }

    /// Only recognized contract codes may survive a confidential failure; never a server string.
    public static func confidentialErrorCode(_ untrustedCode: String?) -> String {
        let allowed: Set<String> = [
            "not_found", "scope_denied", "contract_mismatch", "busy", "cancelled",
            "invalid_argument", "policy_violation", "recorder_unavailable", "engine_unavailable", "internal",
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
        return configuration
    }

    static func permissionConfiguration() -> URLSessionConfiguration {
        let configuration = ordinaryConfiguration()
        configuration.timeoutIntervalForRequest = 150
        configuration.timeoutIntervalForResource = 180
        return configuration
    }

    static func confidentialConfiguration() -> URLSessionConfiguration {
        let configuration = ordinaryConfiguration()
        // Explicitly override system/manual/automatic proxies for this session only.
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

    private static func isIPv4Loopback(_ host: String) -> Bool {
        let parts = host.split(separator: ".", omittingEmptySubsequences: false)
        guard parts.count == 4, parts.first == "127" else { return false }
        return parts.allSatisfy { part in
            !part.isEmpty && part.utf8.allSatisfy { (48...57).contains($0) }
                && (part.count == 1 || part.first != "0") && UInt8(part) != nil
        }
    }

    private static func isIPv6Loopback(_ host: String) -> Bool {
        let literal = host.hasPrefix("[") && host.hasSuffix("]") ? String(host.dropFirst().dropLast()) : host
        var address = in6_addr()
        guard literal.withCString({ inet_pton(AF_INET6, $0, &address) }) == 1 else { return false }
        return withUnsafeBytes(of: address) { bytes in
            bytes.dropLast().allSatisfy { $0 == 0 } && bytes.last == 1
        }
    }
}

private struct ConfidentialEndpointError: Error, LocalizedError {
    var errorDescription: String? { "秘密情報の保存先には、認証情報やクエリを含まない loopback HTTP URL が必要です。" }
}

final class RejectRedirects: NSObject, URLSessionTaskDelegate, Sendable {
    func urlSession(
        _ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse,
        newRequest request: URLRequest, completionHandler: @escaping @Sendable (URLRequest?) -> Void
    ) {
        completionHandler(nil)
    }
}
