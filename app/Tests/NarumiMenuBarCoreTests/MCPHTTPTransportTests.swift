import Foundation
import XCTest

@testable import NarumiMenuBarCore

@MainActor
final class MCPHTTPTransportTests: XCTestCase {
    func testConfidentialEndpointPinsLocalhostAndAcceptsOnlyNumericLoopback() throws {
        let cases = [
            ("http://localhost:8765/mcp", "http://127.0.0.1:8765/mcp"),
            ("http://LOCALHOST:8765/mcp", "http://127.0.0.1:8765/mcp"),
            ("http://127.0.0.1:8765/mcp", "http://127.0.0.1:8765/mcp"),
            ("http://127.2.3.4/mcp", "http://127.2.3.4/mcp"),
            ("http://[::1]:8765/mcp", "http://[::1]:8765/mcp"),
            ("http://[0:0:0:0:0:0:0:1]:8765/mcp", "http://[::1]:8765/mcp"),
        ]
        for (input, expected) in cases {
            XCTAssertEqual(try MCPHTTPTransport.confidentialEndpoint(URL(string: input)).absoluteString, expected)
        }
    }

    func testConfidentialEndpointRejectsRemoteAndAmbiguousURLs() {
        let rejected = [
            "http://example.invalid/mcp", "https://127.0.0.1/mcp", "http://192.168.1.1/mcp",
            "http://0.0.0.0/mcp", "http://localhost.example.invalid/mcp", "http://localhost./mcp",
            "http://2130706433/mcp", "http://127.1/mcp", "http://127.000.0.1/mcp",
            "http://[::ffff:127.0.0.1]/mcp", "http://[::]/mcp", "http://[::1%25lo0]/mcp",
            "http://user:secret@127.0.0.1/mcp", "http://@127.0.0.1/mcp",
            "http://127.0.0.1/mcp?token=secret", "http://127.0.0.1/mcp?",
            "http://127.0.0.1/mcp#secret", "http://127.0.0.1/mcp#",
            "http://127.0.0.1:0/mcp", "http://127.0.0.1:65536/mcp",
        ]
        for value in rejected {
            XCTAssertThrowsError(try MCPHTTPTransport.confidentialEndpoint(URL(string: value)), value) { error in
                XCTAssertFalse(error.localizedDescription.contains(value))
                XCTAssertFalse(error.localizedDescription.contains("secret"))
            }
        }
        XCTAssertThrowsError(try MCPHTTPTransport.confidentialEndpoint(nil))
    }

    func testConfidentialSessionDisablesManualAndAutomaticProxiesAndPersistentStores() {
        let configuration = MCPHTTPTransport.confidentialConfiguration()
        let proxies = configuration.connectionProxyDictionary
        for name in ["HTTPEnable", "HTTPSEnable", "SOCKSEnable", "ProxyAutoConfigEnable", "ProxyAutoDiscoveryEnable"] {
            XCTAssertEqual(proxies?[name] as? Int, 0, name)
        }
        XCTAssertNil(configuration.httpCookieStorage)
        XCTAssertFalse(configuration.httpShouldSetCookies)
        XCTAssertNil(configuration.urlCredentialStorage)
        XCTAssertNil(configuration.urlCache)
        XCTAssertEqual(configuration.requestCachePolicy, .reloadIgnoringLocalCacheData)
    }

    func testSecretToolIsDetectedFromActualRPCBodyWithoutCallerOptIn() throws {
        let url = URL(string: "http://127.0.0.1:8765/mcp")!
        for tool in ToolCatalog.allUsed {
            let request = try rpcRequest(url, tool: tool)
            XCTAssertEqual(MCPHTTPTransport.hasConfidentialBody(request), tool == ToolCatalog.setGaiaConnection)
        }
        // Clearing or preserving a key still belongs to the confidential operation.
        var request = try rpcRequest(url)
        request.httpBody = Data(#"{"method":"tools/call","params":{"name":"set_gaia_connection","arguments":{"api_key":null}}}"#.utf8)
        XCTAssertTrue(MCPHTTPTransport.hasConfidentialBody(request))
    }

    func testActualSecretSendRejectsRemoteEndpointBeforeNetworkIO() async throws {
        let request = try rpcRequest(URL(string: "http://example.invalid/mcp")!)
        do {
            _ = try await MCPHTTPTransport().data(for: request)
            XCTFail("Secret-bearing request must be refused before networking")
        } catch {
            XCTAssertFalse(error.localizedDescription.contains("unit-test-only-secret"))
            XCTAssertFalse(error.localizedDescription.contains("example.invalid"))
        }
    }

    func testActualSecretSendPinsLocalhost() async throws {
        let fixture = try LoopbackHTTPFixture()
        defer { fixture.stop() }
        let request = try rpcRequest(URL(string: "http://localhost:\(fixture.port)/mcp")!)
        let (_, response) = try await MCPHTTPTransport().data(for: request)
        XCTAssertEqual((response as? HTTPURLResponse)?.statusCode, 200)
        let wire = try XCTUnwrap(fixture.requests.first)
        XCTAssertTrue(wire.contains("Host: 127.0.0.1:\(fixture.port)"))
        XCTAssertTrue(wire.contains("unit-test-only-secret"))
    }

    func testActualSecretPOSTNeverFollows307Or308Redirects() async throws {
        for status in [307, 308] {
            let target = try LoopbackHTTPFixture()
            defer { target.stop() }
            let source = try LoopbackHTTPFixture(status: status, location: target.url.absoluteString)
            defer { source.stop() }
            let (_, response) = try await MCPHTTPTransport().data(for: rpcRequest(source.url))
            XCTAssertEqual((response as? HTTPURLResponse)?.statusCode, status)
            XCTAssertEqual(source.requests.count, 1)
            XCTAssertTrue(try XCTUnwrap(source.requests.first).contains("unit-test-only-secret"))
            XCTAssertTrue(target.requests.isEmpty, "\(status) must not forward the secret body")
        }
    }

    func testOrdinaryToolKeepsExistingRedirectBehavior() async throws {
        let target = try LoopbackHTTPFixture()
        defer { target.stop() }
        let source = try LoopbackHTTPFixture(status: 307, location: target.url.absoluteString)
        defer { source.stop() }
        let request = try rpcRequest(source.url, tool: ToolCatalog.getServerInfo)
        let (_, response) = try await MCPHTTPTransport().data(for: request)
        XCTAssertEqual((response as? HTTPURLResponse)?.statusCode, 200)
        XCTAssertEqual(source.requests.count, 1)
        XCTAssertEqual(target.requests.count, 1)
    }

    func testConfidentialHandshakeAlsoUsesTheProtectedTransport() async throws {
        let target = try LoopbackHTTPFixture()
        defer { target.stop() }
        let source = try LoopbackHTTPFixture(status: 307, location: target.url.absoluteString)
        defer { source.stop() }
        var request = URLRequest(url: source.url)
        request.httpMethod = "POST"
        request.httpBody = Data(#"{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}"#.utf8)
        let (_, response) = try await MCPHTTPTransport().data(for: request, protectingSecrets: true)
        XCTAssertEqual((response as? HTTPURLResponse)?.statusCode, 307)
        XCTAssertTrue(target.requests.isEmpty)
    }

    func testOnlyKnownErrorCodesSurviveConfidentialFailures() {
        XCTAssertEqual(MCPHTTPTransport.confidentialErrorCode("invalid_argument"), "invalid_argument")
        XCTAssertEqual(MCPHTTPTransport.confidentialErrorCode("busy"), "busy")
        XCTAssertEqual(MCPHTTPTransport.confidentialErrorCode(nil), "internal")
        XCTAssertEqual(MCPHTTPTransport.confidentialErrorCode("unit-test-only-secret"), "internal")
        XCTAssertFalse(MCPHTTPTransport.confidentialErrorMessage.contains("unit-test-only-secret"))
    }

    func testAppClientRoutesAllHTTPThroughTheTestedTransport() throws {
        // The executable target is not linked into Core tests (it links Sparkle/AppKit).
        // Keep a wiring guard in addition to the real HTTP tests above.
        let app = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
            .deletingLastPathComponent().deletingLastPathComponent()
        let source = try String(contentsOf: app.appendingPathComponent("Sources/NarumiMenuBar/MCPClient.swift"), encoding: .utf8)
        XCTAssertTrue(source.contains("private let transport: MCPHTTPTransport"))
        XCTAssertTrue(source.contains("try await transport.data(for: request, protectingSecrets: confidential)"))
        XCTAssertTrue(source.contains("MCPHTTPTransport.confidentialErrorCode(code)"))
        XCTAssertTrue(source.contains("MCPHTTPTransport.confidentialErrorMessage"))
        XCTAssertFalse(source.contains("URLSession("))
    }

    private func rpcRequest(_ url: URL, tool: String = ToolCatalog.setGaiaConnection) throws -> URLRequest {
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        let arguments: [String: Any] = tool == ToolCatalog.setGaiaConnection
            ? ["api_key": "unit-test-only-secret", "request_id": "test-request-1234"] : [:]
        request.httpBody = try JSONSerialization.data(withJSONObject: [
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": ["name": tool, "arguments": arguments],
        ])
        return request
    }
}
