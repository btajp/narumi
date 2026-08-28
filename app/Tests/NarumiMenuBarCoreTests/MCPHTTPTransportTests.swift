import Foundation
import Security
import XCTest

@testable import NarumiMenuBarCore

@MainActor
final class MCPHTTPTransportTests: XCTestCase {
    private let token = "narumi-public-test-token-never-production"

    func testEndpointAcceptsOnlyExactNumericHTTPSLoopback() throws {
        for input in ["https://127.0.0.1:8765/mcp", "https://[::1]:8765/mcp"] {
            XCTAssertEqual(try MCPHTTPTransport.confidentialEndpoint(URL(string: input)).absoluteString, input)
        }
    }

    func testEndpointRejectsHTTPDNSRemoteAndAmbiguousURLsForEveryTool() throws {
        let rejected = [
            "http://127.0.0.1:8765/mcp", "https://localhost:8765/mcp", "https://127.0.0.1/mcp",
            "https://example.invalid:8765/mcp", "https://192.168.1.1:8765/mcp",
            "https://127.2.3.4:8765/mcp", "https://localhost.:8765/mcp",
            "https://2130706433:8765/mcp", "https://127.1:8765/mcp", "https://127.000.0.1:8765/mcp",
            "https://[::ffff:127.0.0.1]:8765/mcp", "https://[::]:8765/mcp",
            "https://[0:0:0:0:0:0:0:1]:8765/mcp", "https://[::1%25lo0]:8765/mcp",
            "https://user:secret@127.0.0.1:8765/mcp", "https://@127.0.0.1:8765/mcp",
            "https://127.0.0.1:8765/mcp?token=secret", "https://127.0.0.1:8765/mcp?",
            "https://127.0.0.1:8765/mcp#secret", "https://127.0.0.1:8765/mcp#",
            "https://127.0.0.1:0/mcp", "https://127.0.0.1:65536/mcp",
            "https://127.0.0.1:8765/mcp/", "https://127.0.0.1:8765/%6dcp",
        ]
        for value in rejected {
            for tool in [ToolCatalog.getServerInfo, ToolCatalog.setProviderConnection] {
                let request = try rpcRequest(URL(string: value)!, tool: tool)
                XCTAssertThrowsError(try MCPHTTPTransport.requestPlan(for: request), value) { error in
                    XCTAssertFalse(error.localizedDescription.contains(value))
                    XCTAssertFalse(error.localizedDescription.contains("secret"))
                }
            }
        }
        XCTAssertThrowsError(try MCPHTTPTransport.confidentialEndpoint(nil))
    }

    func testEverySessionDisablesProxiesAndPersistentStores() {
        for configuration in [MCPHTTPTransport.ordinaryConfiguration(), MCPHTTPTransport.permissionConfiguration(),
            MCPHTTPTransport.confidentialConfiguration()] {
            for name in ["HTTPEnable", "HTTPSEnable", "SOCKSEnable", "ProxyAutoConfigEnable", "ProxyAutoDiscoveryEnable"] {
                XCTAssertEqual(configuration.connectionProxyDictionary?[name] as? Int, 0, name)
            }
            XCTAssertNil(configuration.httpCookieStorage)
            XCTAssertFalse(configuration.httpShouldSetCookies)
            XCTAssertNil(configuration.urlCredentialStorage)
            XCTAssertNil(configuration.urlCache)
            XCTAssertEqual(configuration.requestCachePolicy, .reloadIgnoringLocalCacheData)
        }
    }

    func testSecretToolIsDetectedFromRPCBodyWithoutCallerOptIn() throws {
        let secrets: Set<String> = [ToolCatalog.setGaiaConnection, ToolCatalog.setProviderConnection,
            ToolCatalog.authenticateProviderConnection, ToolCatalog.deleteProviderConnection]
        for tool in ToolCatalog.allUsed {
            let request = try rpcRequest(URL(string: "https://127.0.0.1:8765/mcp")!, tool: tool)
            XCTAssertEqual(MCPHTTPTransport.hasConfidentialBody(request), secrets.contains(tool), tool)
        }
    }

    func testActualTLSDiscoveryAndSecretRequestUsePinnedCertificateAndBearer() async throws {
        let fixture = try LoopbackTLSFixture()
        defer { fixture.stop() }
        let transport = try makeTransport(url: fixture.url)
        for tool in [ToolCatalog.getServerInfo, ToolCatalog.setProviderConnection] {
            let (_, response) = try await transport.data(for: rpcRequest(fixture.url, tool: tool))
            XCTAssertEqual((response as? HTTPURLResponse)?.statusCode, 200)
        }
        XCTAssertEqual(fixture.requests.count, 2)
        for wire in fixture.requests {
            XCTAssertTrue(wire.contains("Host: 127.0.0.1:\(fixture.port)"))
            XCTAssertTrue(wire.contains("Authorization: Bearer \(token)"))
        }
        XCTAssertTrue(fixture.requests[1].contains("unit-test-only-secret"))
    }

    func testFalseServerCertificateReceivesNeitherTokenNorSecretBody() async throws {
        let fixture = try LoopbackTLSFixture()
        defer { fixture.stop() }
        var otherCertificate = LoopbackTLSCertificate.certificateDER
        otherCertificate[otherCertificate.count - 1] ^= 1
        let transport = try makeTransport(url: fixture.url, certificate: otherCertificate)
        do {
            _ = try await transport.data(for: rpcRequest(fixture.url))
            XCTFail("A different certificate must fail before HTTP bytes are transmitted")
        } catch {
            XCTAssertEqual(error as? MCPConnectionError, .certificateMismatch)
            XCTAssertFalse(error.localizedDescription.contains(token))
        }
        XCTAssertTrue(fixture.requests.isEmpty)
    }

    func testPlaintextAndUnpinnedAlternateEndpointFailBeforeNetworking() async throws {
        let http = try LoopbackHTTPFixture()
        defer { http.stop() }
        let transport = try makeTransport(url: URL(string: "https://127.0.0.1:8765/mcp")!)
        for url in [http.url, URL(string: "https://127.0.0.1:\(http.port)/mcp")!] {
            do {
                _ = try await transport.data(for: rpcRequest(url, tool: ToolCatalog.getServerInfo))
                XCTFail("An unpinned endpoint must never be contacted")
            } catch {
                XCTAssertNotNil(error as? MCPConnectionError)
            }
        }
        XCTAssertTrue(http.requests.isEmpty)
    }

    func testNoToolOrHandshakeFollowsRedirects() async throws {
        for status in [302, 307, 308] {
            let target = try LoopbackTLSFixture()
            defer { target.stop() }
            let source = try LoopbackTLSFixture(status: status, location: target.url.absoluteString)
            defer { source.stop() }
            let transport = try makeTransport(url: source.url)
            for tool in [ToolCatalog.getServerInfo, ToolCatalog.setProviderConnection] {
                let (_, response) = try await transport.data(for: rpcRequest(source.url, tool: tool))
                XCTAssertEqual((response as? HTTPURLResponse)?.statusCode, status)
            }
            var initialization = URLRequest(url: source.url)
            initialization.httpMethod = "POST"
            initialization.httpBody = Data(#"{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}"#.utf8)
            let (_, response) = try await transport.data(for: initialization)
            XCTAssertEqual((response as? HTTPURLResponse)?.statusCode, status)
            XCTAssertEqual(source.requests.count, 3)
            XCTAssertTrue(target.requests.isEmpty)
        }
    }

    func testCallerCannotOverrideDestinationHeadersOrBearer() async throws {
        let fixture = try LoopbackTLSFixture()
        defer { fixture.stop() }
        var request = try rpcRequest(fixture.url)
        request.setValue("evil.invalid", forHTTPHeaderField: "Host")
        request.setValue("https://evil.invalid", forHTTPHeaderField: "Origin")
        request.setValue("caller-secret", forHTTPHeaderField: "Authorization")
        request.setValue("caller-secret", forHTTPHeaderField: "Cookie")
        request.setValue("caller-secret", forHTTPHeaderField: "Proxy-Authorization")
        _ = try await makeTransport(url: fixture.url).data(for: request)
        let wire = try XCTUnwrap(fixture.requests.first)
        XCTAssertFalse(wire.contains("evil.invalid"))
        XCTAssertFalse(wire.contains("caller-secret"))
        XCTAssertTrue(wire.contains("Authorization: Bearer \(token)"))
    }

    func testPinnedCertificateMustAlsoBeCurrentlyValid() throws {
        let bootstrap = try makeBootstrap(url: URL(string: "https://127.0.0.1:8765/mcp")!)
        let certificate = try XCTUnwrap(SecCertificateCreateWithData(nil, try bootstrap.certificateDER() as CFData))
        var trust: SecTrust?
        XCTAssertEqual(SecTrustCreateWithCertificates(certificate, SecPolicyCreateBasicX509(), &trust), errSecSuccess)
        let evaluated = try XCTUnwrap(trust)
        let delegate = MCPPinnedServerTrust(bootstrap: bootstrap)
        XCTAssertTrue(delegate.accepts(evaluated))
        SecTrustSetVerifyDate(evaluated, Date(timeIntervalSince1970: 7_258_118_400) as CFDate)
        XCTAssertFalse(delegate.accepts(evaluated), "A matching pin must not bypass expiration")
    }

    func testOnlyKnownErrorCodesSurviveConfidentialFailures() {
        for code in ["invalid_argument", "busy", "authentication_required", "configuration_conflict"] {
            XCTAssertEqual(MCPHTTPTransport.confidentialErrorCode(code), code)
        }
        XCTAssertEqual(MCPHTTPTransport.confidentialErrorCode(nil), "internal")
        XCTAssertEqual(MCPHTTPTransport.confidentialErrorCode("unit-test-only-secret"), "internal")
    }

    func testAppClientRoutesHTTPThroughAuthenticatedTransportAndChecksMetadataGate() throws {
        let app = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
            .deletingLastPathComponent().deletingLastPathComponent()
        let source = try String(contentsOf: app.appendingPathComponent("Sources/NarumiMenuBar/MCPClient.swift"), encoding: .utf8)
        let connection = try String(
            contentsOf: app.appendingPathComponent("Sources/NarumiMenuBar/MCPClient+Connection.swift"), encoding: .utf8)
        XCTAssertTrue(source.contains("try await transport.data(for: request, protectingSecrets: confidential)"))
        XCTAssertTrue(source.contains("MCPHTTPTransport.confidentialErrorCode(code)"))
        XCTAssertTrue(source.contains("[\"client_auth_required\"]?.boolValue == true"))
        XCTAssertTrue(connection.contains("MCPServerBootstrapReader("))
        XCTAssertTrue(connection.contains("KeychainHelperSecretReader("))
        XCTAssertFalse(source.contains("URLSession("))
    }

    private func makeBootstrap(url: URL, certificate: Data = LoopbackTLSCertificate.certificateDER) throws -> MCPServerBootstrap {
        let instance = "00000000-0000-4000-8000-000000000001"
        return MCPServerBootstrap(
            serverInstanceID: instance, pid: 1, url: url,
            certificateSHA256: MCPServerBootstrap.fingerprint(certificate),
            certificatePEM: "-----BEGIN CERTIFICATE-----\n\(certificate.base64EncodedString())\n-----END CERTIFICATE-----\n",
            tokenAccount: "transport:\(String(repeating: "a", count: 64)):\(instance)")
    }

    private func makeTransport(url: URL, certificate: Data = LoopbackTLSCertificate.certificateDER) throws -> MCPHTTPTransport {
        try MCPHTTPTransport(connection: MCPServerConnection(bootstrap: makeBootstrap(url: url, certificate: certificate), token: token))
    }

    private func rpcRequest(_ url: URL, tool: String = ToolCatalog.setProviderConnection) throws -> URLRequest {
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        let arguments: [String: Any] = MCPHTTPTransport.isConfidentialTool(tool)
            ? ["api_key": "unit-test-only-secret", "request_id": "test-request-1234"] : [:]
        request.httpBody = try JSONSerialization.data(withJSONObject: [
            "jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": ["name": tool, "arguments": arguments],
        ])
        return request
    }
}
