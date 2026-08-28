import CryptoKit
import Foundation
import Security

/// Public certificate material, read only from the owner-controlled bootstrap file.
public struct MCPServerBootstrap: Codable, Equatable, Sendable {
    public let version: Int
    public let serverInstanceID: String
    public let pid: Int32
    public let url: URL
    public let certificateSHA256: String
    public let certificatePEM: String
    public let tokenAccount: String

    enum CodingKeys: String, CodingKey {
        case version, pid, url
        case serverInstanceID = "server_instance_id"
        case certificateSHA256 = "certificate_sha256"
        case certificatePEM = "certificate_pem"
        case tokenAccount = "token_account"
    }

    public init(
        version: Int = 1, serverInstanceID: String, pid: Int32, url: URL,
        certificateSHA256: String, certificatePEM: String, tokenAccount: String
    ) {
        self.version = version
        self.serverInstanceID = serverInstanceID
        self.pid = pid
        self.url = url
        self.certificateSHA256 = certificateSHA256
        self.certificatePEM = certificatePEM
        self.tokenAccount = tokenAccount
    }

    public func validate(expectedURL: URL) throws {
        _ = try MCPServerEndpoint.validate(expectedURL)
        guard try MCPServerEndpoint.validate(url) == expectedURL else {
            throw MCPConnectionError.endpointMismatch
        }
        guard version == 1, pid > 0,
            RecordingPermissionContract.isValidServerInstanceID(serverInstanceID),
            certificateSHA256.count == 64,
            certificateSHA256.utf8.allSatisfy({ (48...57).contains($0) || (97...102).contains($0) }),
            tokenAccount.hasPrefix("transport:"), tokenAccount.count <= 256,
            tokenAccount.utf8.allSatisfy({ (33...126).contains($0) }),
            tokenAccount.hasSuffix(":" + serverInstanceID)
        else { throw MCPConnectionError.invalidBootstrap }
        let der = try certificateDER()
        guard Self.fingerprint(der) == certificateSHA256,
            SecCertificateCreateWithData(nil, der as CFData) != nil
        else { throw MCPConnectionError.invalidBootstrap }
    }

    public func certificateDER() throws -> Data {
        let lines = certificatePEM.split(whereSeparator: { $0.isNewline })
        guard lines.count >= 3, lines.first == "-----BEGIN CERTIFICATE-----",
            lines.last == "-----END CERTIFICATE-----",
            let der = Data(base64Encoded: lines.dropFirst().dropLast().joined()),
            !der.isEmpty, der.count < 16_384
        else { throw MCPConnectionError.invalidBootstrap }
        return der
    }

    public static func fingerprint(_ certificateDER: Data) -> String {
        SHA256.hash(data: certificateDER).map { String(format: "%02x", $0) }.joined()
    }
}

/// The bearer token is deliberately excluded from public diagnostics and descriptions.
public struct MCPServerConnection: Sendable, CustomStringConvertible, CustomDebugStringConvertible {
    public let bootstrap: MCPServerBootstrap
    private let token: String

    public init(bootstrap: MCPServerBootstrap, token: String) throws {
        try bootstrap.validate(expectedURL: bootstrap.url)
        let allowed = Set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-._~+/=".utf8)
        guard (32...256).contains(token.utf8.count), token.utf8.allSatisfy(allowed.contains) else {
            throw MCPConnectionError.credentialUnavailable
        }
        self.bootstrap = bootstrap
        self.token = token
    }

    var authorization: String { "Bearer " + token }
    public var description: String { "MCPServerConnection(authenticated, credentials hidden)" }
    public var debugDescription: String { description }
}
