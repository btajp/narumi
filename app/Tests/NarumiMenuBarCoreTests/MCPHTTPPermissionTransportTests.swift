import Foundation
import XCTest

@testable import NarumiMenuBarCore

final class MCPHTTPPermissionTransportTests: XCTestCase {
    func testPermissionSessionExtendsBothDeadlinesWithoutChangingOtherSessions() {
        let cases: [(URLSessionConfiguration, TimeInterval, TimeInterval)] = [
            (MCPHTTPTransport.ordinaryConfiguration(), 30, 120),
            (MCPHTTPTransport.confidentialConfiguration(), 30, 120),
            (MCPHTTPTransport.permissionConfiguration(), 150, 180),
        ]
        for (configuration, requestTimeout, resourceTimeout) in cases {
            XCTAssertEqual(configuration.timeoutIntervalForRequest, requestTimeout)
            XCTAssertEqual(configuration.timeoutIntervalForResource, resourceTimeout)
        }
    }

    func testOnlyPermissionToolGetsExtendedSessionAndPerRequestTimeout() throws {
        for tool in ToolCatalog.allUsed {
            let request = try rpcRequest(tool: tool)
            let plan = try MCPHTTPTransport.requestPlan(for: request)
            let expectedRoute: MCPHTTPTransport.RequestRoute = tool == ToolCatalog.setGaiaConnection
                ? .confidential : (tool == ToolCatalog.configureRecordingPermission ? .permissionSetup : .ordinary)
            XCTAssertEqual(plan.route, expectedRoute, tool)
            XCTAssertEqual(plan.request.timeoutInterval, tool == ToolCatalog.configureRecordingPermission ? 150 : 30)
            XCTAssertEqual(plan.request.httpBody, request.httpBody)
            XCTAssertEqual(plan.request.httpMethod, request.httpMethod)
            XCTAssertEqual(plan.request.value(forHTTPHeaderField: "Mcp-Session-Id"), "permission-fixture")
            XCTAssertEqual(request.timeoutInterval, 30, "The caller's request must not be mutated")
        }
    }

    func testMalformedOrNonCallBodiesCannotSelectPermissionSession() throws {
        let bodies: [String?] = [
            nil, "invalid JSON", "[]", "{}",
            #"{"method":"initialize","params":{"name":"configure_recording_permission"}}"#,
            #"{"method":"tools/list","params":{"name":"configure_recording_permission"}}"#,
            #"{"method":"tools/call","params":{"name":"configure_recording_permission_extra"}}"#,
            #"{"method":"tools/call","params":{"name":false}}"#,
            #"{"params":{"name":"configure_recording_permission"}}"#,
        ]
        for body in bodies {
            var request = try rpcRequest(tool: ToolCatalog.configureRecordingPermission)
            request.httpBody = body.map { Data($0.utf8) }
            let plan = try MCPHTTPTransport.requestPlan(for: request)
            XCTAssertEqual(plan.route, .ordinary)
            XCTAssertEqual(plan.request.timeoutInterval, 30)
        }
    }

    func testExplicitSecretProtectionTakesPriorityOverPermissionRouting() throws {
        var request = try rpcRequest(tool: ToolCatalog.configureRecordingPermission)
        request.url = URL(string: "http://localhost:8765/mcp")
        request.httpShouldHandleCookies = true
        let plan = try MCPHTTPTransport.requestPlan(for: request, protectingSecrets: true)
        XCTAssertEqual(plan.route, .confidential)
        XCTAssertEqual(plan.request.url?.host, "127.0.0.1")
        XCTAssertFalse(plan.request.httpShouldHandleCookies)
        XCTAssertEqual(plan.request.cachePolicy, .reloadIgnoringLocalCacheData)
        XCTAssertEqual(plan.request.timeoutInterval, 30)
        request.url = URL(string: "http://example.invalid/mcp")
        XCTAssertThrowsError(try MCPHTTPTransport.requestPlan(for: request, protectingSecrets: true))
    }

    private func rpcRequest(tool: String) throws -> URLRequest {
        var request = URLRequest(url: URL(string: "http://127.0.0.1:8765/mcp")!, timeoutInterval: 30)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue("permission-fixture", forHTTPHeaderField: "Mcp-Session-Id")
        let rpc: [String: Any] = [
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": ["name": tool, "arguments": [String: Any]()] as [String: Any],
        ]
        request.httpBody = try JSONSerialization.data(withJSONObject: rpc)
        return request
    }
}
