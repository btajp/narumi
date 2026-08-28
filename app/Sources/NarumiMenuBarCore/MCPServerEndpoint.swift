import Foundation

/// Resident MCP has one exact, numeric loopback endpoint. No DNS or HTTP downgrade is used.
public enum MCPServerEndpoint {
    public static func validate(_ url: URL?) throws -> URL {
        guard let url, let components = URLComponents(url: url, resolvingAgainstBaseURL: false),
            components.scheme == "https", components.user == nil, components.password == nil,
            components.query == nil, components.fragment == nil,
            components.percentEncodedPath == ServerConfig.mcpPath,
            let port = components.port, (1...65535).contains(port),
            let host = components.host,
            ["127.0.0.1", "[::1]", "::1"].contains(host),
            components.percentEncodedHost?.contains("%") != true
        else { throw MCPConnectionError.invalidEndpoint }
        let numericHost = host == "127.0.0.1" ? host : "[::1]"
        guard url.absoluteString == "https://\(numericHost):\(port)\(ServerConfig.mcpPath)" else {
            throw MCPConnectionError.invalidEndpoint
        }
        return url
    }

    static func trustHost(_ url: URL) -> String {
        url.host == "127.0.0.1" ? "127.0.0.1" : "::1"
    }
}

/// Errors never include file contents, tokens, URLs supplied by a peer, or OS exception text.
public enum MCPConnectionError: Error, Equatable, LocalizedError {
    case invalidEndpoint
    case bootstrapUnavailable
    case unsafeBootstrap
    case invalidBootstrap
    case endpointMismatch
    case serverUnavailable
    case credentialUnavailable
    case certificateMismatch
    case connectionChanged
    case incompatibleContract
    case transportFailed

    public var errorDescription: String? {
        switch self {
        case .invalidEndpoint:
            return "接続先には数値 loopback の HTTPS MCP URL が必要です。HTTP には接続しません。"
        case .bootstrapUnavailable:
            return "サーバーの認証用起動情報がありません。サーバーを起動して再確認してください。"
        case .unsafeBootstrap:
            return "サーバーの起動情報の所有者またはアクセス権を確認できないため、接続を中止しました。"
        case .invalidBootstrap:
            return "サーバーの認証用起動情報を検証できないため、接続を中止しました。"
        case .endpointMismatch:
            return "設定した接続先とサーバーの認証用起動情報が一致しません。"
        case .serverUnavailable:
            return "認証用起動情報に記録されたサーバーが動作していません。"
        case .credentialUnavailable:
            return "サーバーの接続資格情報を安全に取得できません。認証ヘルパーを確認してください。"
        case .certificateMismatch:
            return "サーバー証明書が起動情報と一致しないため、通信を中止しました。"
        case .connectionChanged:
            return "サーバーの接続が切り替わりました。状態を再確認してから操作してください。"
        case .incompatibleContract:
            return "サーバーの契約バージョンに対応していません。契約 2 のサーバーが必要です。"
        case .transportFailed:
            return "認証済みサーバーに接続できません。接続状態を確認してください。"
        }
    }
}
