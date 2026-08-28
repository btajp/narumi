import XCTest

@testable import NarumiMenuBarCore

final class MCPPermissionSessionStateTests: XCTestCase {
    private let instanceA = "00000000-0000-4000-8000-000000000001"
    private let instanceB = "00000000-0000-4000-8000-000000000002"

    func testInitialSessionAllowsOnlyTheEmptyServerInfoProbe() {
        let state = MCPPermissionSessionState()
        XCTAssertEqual(state.generation, 0)
        XCTAssertNil(state.contractVersion)
        XCTAssertNil(state.serverInstanceID)
        XCTAssertTrue(state.allowsCall(tool: ToolCatalog.getServerInfo))
        XCTAssertFalse(state.allowsCall(tool: ToolCatalog.getServerInfo, refreshingPermissions: true))
        XCTAssertFalse(state.allowsCall(tool: ToolCatalog.configureRecordingPermission))
        assertOnlyEmptyProbeAllowed(state)
    }

    func testCurrentSessionObservationEnablesCallsOnlyForKnownAuthenticatedContract() {
        var state = MCPPermissionSessionState()
        XCTAssertTrue(state.observeServerInfo(
            contractVersion: "2.0.0", serverInstanceID: instanceA, requestGeneration: state.generation))
        XCTAssertEqual(state.contractVersion, "2.0.0")
        XCTAssertEqual(state.serverInstanceID, instanceA)
        XCTAssertEqual(state.generation, 0, "The initial identity observation must not invalidate its own request")
        XCTAssertTrue(state.allowsCall(tool: ToolCatalog.configureRecordingPermission))
        XCTAssertTrue(state.allowsCall(tool: ToolCatalog.getServerInfo, refreshingPermissions: true))
        XCTAssertTrue(state.allowsCall(tool: ToolCatalog.getServerInfo))
        for tool in ToolCatalog.allUsed {
            XCTAssertTrue(state.allowsCall(tool: tool), tool)
        }

        XCTAssertTrue(state.observeServerInfo(contractVersion: nil, requestGeneration: state.generation))
        XCTAssertNil(state.contractVersion)
        XCTAssertNil(state.serverInstanceID)
        assertOnlyEmptyProbeAllowed(state)
    }

    func testResetRevokesSupportAndRejectsOlderSessionSnapshot() {
        var state = MCPPermissionSessionState()
        let oldGeneration = state.generation
        state.observeServerInfo(contractVersion: "2.0.0", serverInstanceID: instanceA, requestGeneration: oldGeneration)
        state.reset()
        XCTAssertEqual(state.generation, oldGeneration + 1)
        XCTAssertNil(state.contractVersion)
        XCTAssertNil(state.serverInstanceID)
        assertOnlyEmptyProbeAllowed(state)
        XCTAssertFalse(state.observeServerInfo(
            contractVersion: "2.1.0", serverInstanceID: instanceA, requestGeneration: oldGeneration))
        XCTAssertNil(state.contractVersion)

        state.observeServerInfo(contractVersion: "1.1.0", serverInstanceID: instanceB, requestGeneration: state.generation)
        XCTAssertFalse(state.observeServerInfo(
            contractVersion: "2.0.0", serverInstanceID: instanceA, requestGeneration: oldGeneration))
        XCTAssertEqual(state.contractVersion, "1.1.0")
        XCTAssertEqual(state.serverInstanceID, instanceB)
        assertOnlyEmptyProbeAllowed(state)
    }

    func testUnknownOldAndUnsupportedMajorVersionsCannotAuthorizeDataCalls() {
        let unsupported: [String?] = [
            nil, "", "malformed", "1.0.0", "1.1.0", "2.0.0-rc.1", "0.9.0",
            "3.0.0-rc.1", "4.0.0-rc.1", "5.0.0",
        ]
        for version in unsupported {
            var state = MCPPermissionSessionState()
            XCTAssertTrue(state.observeServerInfo(
                contractVersion: version, serverInstanceID: instanceA, requestGeneration: state.generation))
            assertOnlyEmptyProbeAllowed(state)
        }
    }

    func testKnownNewVersionWithoutValidIdentityDoesNotAuthorizeDataCalls() {
        let unknownIDs: [String?] = [nil, "invalid", "12345678-9ABC-4DEF-8ABC-123456789ABC"]
        for instanceID in unknownIDs {
            var state = MCPPermissionSessionState()
            state.observeServerInfo(contractVersion: "2.0.0", serverInstanceID: instanceID, requestGeneration: state.generation)
            XCTAssertNil(state.serverInstanceID)
            assertOnlyEmptyProbeAllowed(state)
        }
    }

    func testMissingIdentityInvalidatesPreparedGenerationAndIgnoresOldResponse() {
        var state = MCPPermissionSessionState()
        state.observeServerInfo(contractVersion: "2.0.0", serverInstanceID: instanceA, requestGeneration: state.generation)
        let preparedGeneration = state.generation
        XCTAssertTrue(state.observeServerInfo(contractVersion: "2.0.0", requestGeneration: preparedGeneration))
        XCTAssertEqual(state.generation, preparedGeneration + 1)
        XCTAssertNil(state.serverInstanceID)
        assertOnlyEmptyProbeAllowed(state)
        XCTAssertFalse(state.observeServerInfo(
            contractVersion: "2.0.0", serverInstanceID: instanceA, requestGeneration: preparedGeneration))
        XCTAssertNil(state.serverInstanceID)
    }

    func testFakeSendsOrdinaryProbeWhileFreshProbeWaitsForCurrentSessionVersion() {
        var client = FakeClient()
        XCTAssertTrue(client.send(tool: ToolCatalog.getServerInfo))
        XCTAssertFalse(client.send(tool: ToolCatalog.getServerInfo, refreshingPermissions: true))
        XCTAssertFalse(client.send(tool: ToolCatalog.configureRecordingPermission))
        XCTAssertFalse(client.send(tool: ToolCatalog.setProviderConnection))
        XCTAssertFalse(client.send(tool: ToolCatalog.listProviderConnections))
        XCTAssertEqual(client.sends, 1)

        client.state.observeServerInfo(
            contractVersion: "2.0.0", serverInstanceID: instanceA, requestGeneration: client.state.generation)
        XCTAssertTrue(client.send(tool: ToolCatalog.getServerInfo, refreshingPermissions: true))
        XCTAssertTrue(client.send(tool: ToolCatalog.configureRecordingPermission))
        XCTAssertEqual(client.sends, 3)

        client.state.reset()
        XCTAssertFalse(client.send(tool: ToolCatalog.getServerInfo, refreshingPermissions: true))
        XCTAssertFalse(client.send(tool: ToolCatalog.configureRecordingPermission))
        XCTAssertFalse(client.send(tool: ToolCatalog.listProviders))
        XCTAssertFalse(client.send(tool: ToolCatalog.setProviderConnection))
        XCTAssertTrue(client.send(tool: ToolCatalog.getServerInfo))
        XCTAssertEqual(client.sends, 4)
    }

    func testGenerationChangePreventsPreparedSendEvenWhenNewSessionAlsoSupportsSetup() {
        var client = FakeClient()
        client.state.observeServerInfo(
            contractVersion: "2.0.0", serverInstanceID: instanceA, requestGeneration: client.state.generation)
        let preparedGeneration = client.state.generation
        client.state.reset()
        client.state.observeServerInfo(
            contractVersion: "2.0.0", serverInstanceID: instanceB, requestGeneration: client.state.generation)

        XCTAssertFalse(client.send(tool: ToolCatalog.configureRecordingPermission, preparedGeneration: preparedGeneration))
        XCTAssertFalse(client.send(tool: ToolCatalog.authenticateProviderConnection, preparedGeneration: preparedGeneration))
        XCTAssertFalse(client.send(
            tool: ToolCatalog.getServerInfo, refreshingPermissions: true, preparedGeneration: preparedGeneration))
        XCTAssertEqual(client.sends, 0)
        XCTAssertTrue(client.send(tool: ToolCatalog.configureRecordingPermission))
        XCTAssertEqual(client.sends, 1)
    }

    func testInstanceChangeInSameMCPSessionInvalidatesPreparedOperations() {
        var client = FakeClient()
        client.state.observeServerInfo(
            contractVersion: "2.0.0", serverInstanceID: instanceA, requestGeneration: client.state.generation)
        let preparedGeneration = client.state.generation
        client.state.observeServerInfo(
            contractVersion: "2.0.0", serverInstanceID: instanceA, requestGeneration: preparedGeneration)
        XCTAssertEqual(client.state.generation, preparedGeneration)

        XCTAssertTrue(client.state.observeServerInfo(
            contractVersion: "2.0.0", serverInstanceID: instanceB, requestGeneration: preparedGeneration))
        XCTAssertEqual(client.state.generation, preparedGeneration + 1)
        XCTAssertEqual(client.state.serverInstanceID, instanceB)
        XCTAssertFalse(client.send(tool: ToolCatalog.configureRecordingPermission, preparedGeneration: preparedGeneration))
        XCTAssertFalse(client.send(tool: ToolCatalog.setProviderConnection, preparedGeneration: preparedGeneration))
        XCTAssertFalse(client.send(
            tool: ToolCatalog.getServerInfo, refreshingPermissions: true, preparedGeneration: preparedGeneration))
        XCTAssertFalse(client.state.observeServerInfo(
            contractVersion: "2.0.0", serverInstanceID: instanceA, requestGeneration: preparedGeneration))
        XCTAssertEqual(client.state.serverInstanceID, instanceB)
        XCTAssertEqual(client.sends, 0)
        XCTAssertTrue(client.send(tool: ToolCatalog.configureRecordingPermission))
    }

    private func assertOnlyEmptyProbeAllowed(
        _ state: MCPPermissionSessionState, file: StaticString = #filePath, line: UInt = #line
    ) {
        XCTAssertTrue(state.allowsCall(tool: ToolCatalog.getServerInfo), file: file, line: line)
        XCTAssertFalse(state.allowsCall(tool: ToolCatalog.getServerInfo, refreshingPermissions: true), file: file, line: line)
        for tool in ToolCatalog.allUsed + ["future_unknown_tool"] where tool != ToolCatalog.getServerInfo {
            XCTAssertFalse(state.allowsCall(tool: tool), tool, file: file, line: line)
        }
    }

    private struct FakeClient {
        var state = MCPPermissionSessionState()
        private(set) var sends = 0

        mutating func send(
            tool: String, refreshingPermissions: Bool = false, preparedGeneration: UInt64? = nil
        ) -> Bool {
            guard preparedGeneration == nil || preparedGeneration == state.generation,
                state.allowsCall(tool: tool, refreshingPermissions: refreshingPermissions)
            else { return false }
            sends += 1
            return true
        }
    }
}
