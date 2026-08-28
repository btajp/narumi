import Foundation
import XCTest

@testable import NarumiMenuBarCore

final class MCPToolReplayPolicyTests: XCTestCase {
    func testOnlyKnownLocalReadsCanRetrySession() {
        let reads: Set<String> = [
            ToolCatalog.getServerInfo, ToolCatalog.getRecordingStatus, ToolCatalog.listMeetings,
            ToolCatalog.searchTranscripts, ToolCatalog.getMeeting, ToolCatalog.getMinutes,
            ToolCatalog.getTranscript, ToolCatalog.listExportDestinations, ToolCatalog.getJobStatus,
            ToolCatalog.listProfiles, ToolCatalog.getProfile, ToolCatalog.getGaiaConnection,
            ToolCatalog.listProviders, ToolCatalog.listProviderConnections, ToolCatalog.getProviderAuthStatus,
        ]
        for tool in ToolCatalog.allUsed + ["future_unknown_tool", "get_unknown_provider_data"] {
            for refreshing in [false, true] {
                let expected = reads.contains(tool)
                    && (tool != ToolCatalog.getServerInfo || !refreshing)
                XCTAssertEqual(
                    MCPToolReplayPolicy.allowsSessionRetry(tool: tool, refreshingPermissions: refreshing),
                    expected, tool)
            }
        }
    }

    func testExistingJobAdmissionRulesPreserveDefaultsAndExplicitFlags() {
        let cases: [(tool: String, autoProcess: Bool?, autoRegenerate: Bool?, expected: Bool)] = [
            (ToolCatalog.regenerate, nil, nil, true),
            (ToolCatalog.regenerate, false, false, true),
            (ToolCatalog.exportMinutes, nil, nil, true),
            (ToolCatalog.exportMinutes, false, false, true),
            (ToolCatalog.prepareProviderRuntime, nil, nil, true),
            (ToolCatalog.prepareProviderRuntime, false, false, true),
            (ToolCatalog.importRecording, nil, nil, true),
            (ToolCatalog.importRecording, true, false, true),
            (ToolCatalog.importRecording, false, true, false),
            (ToolCatalog.stopRecording, nil, nil, true),
            (ToolCatalog.stopRecording, true, false, true),
            (ToolCatalog.stopRecording, false, true, false),
            (ToolCatalog.registerContext, nil, nil, false),
            (ToolCatalog.registerContext, true, false, false),
            (ToolCatalog.registerContext, false, true, true),
        ]
        for row in cases {
            XCTAssertEqual(
                MCPToolReplayPolicy.createsJob(
                    tool: row.tool, autoProcess: row.autoProcess, autoRegenerate: row.autoRegenerate),
                row.expected, row.tool)
        }
    }

    func testNonJobToolsNeverEnterRecoveryRegardlessOfFlags() {
        let jobTools: Set<String> = [
            ToolCatalog.regenerate, ToolCatalog.exportMinutes, ToolCatalog.importRecording,
            ToolCatalog.stopRecording, ToolCatalog.registerContext, ToolCatalog.prepareProviderRuntime,
        ]
        let flags: [Bool?] = [nil, false, true]
        for tool in ToolCatalog.allUsed + ["future_unknown_tool"] where !jobTools.contains(tool) {
            for autoProcess in flags {
                for autoRegenerate in flags {
                    XCTAssertFalse(
                        MCPToolReplayPolicy.createsJob(
                            tool: tool, autoProcess: autoProcess, autoRegenerate: autoRegenerate), tool)
                }
            }
        }
    }

    func testLostPermissionResponseDoesNotResendOn404OrRepeatedStatusTicks() {
        for failure in FakeClient.Failure.allCases {
            var client = FakeClient()
            client.call(tool: ToolCatalog.configureRecordingPermission, failure: failure)
            XCTAssertEqual(client.sends, 1)
            XCTAssertEqual(client.sessionRetries, 0)
            XCTAssertEqual(client.pendingJobs, 0)
            for _ in 0..<100 {
                client.statusTick()
            }
            XCTAssertEqual(client.sends, 1, "A lost permission response must await explicit user retry")
        }
    }

    func testFreshPermissionProbeIsNotReplayedAfter404OrStatusTicks() {
        var client = FakeClient()
        let arguments = RecordingPermissionContract.serverInfoArguments(
            contractVersion: "2.0.0", serverInstanceID: "00000000-0000-4000-8000-000000000001",
            refreshPermissions: true)
        client.call(tool: ToolCatalog.getServerInfo, failure: .http404, arguments: arguments)
        for _ in 0..<100 {
            client.statusTick()
        }
        XCTAssertEqual(client.sends, 1)
        XCTAssertEqual(client.sessionRetries, 0)
        XCTAssertEqual(client.pendingJobs, 0)
        XCTAssertEqual(client.sentArguments, [["refresh_permissions": true]])
    }

    func testProviderMutationsRefreshTestsAndSetupNeverAutomaticallyResend() {
        let tools = [
            ToolCatalog.setProviderConnection, ToolCatalog.deleteProviderConnection,
            ToolCatalog.authenticateProviderConnection, ToolCatalog.testProviderConnection,
            ToolCatalog.listProviderModels, ToolCatalog.prepareProviderRuntime, ToolCatalog.testGaiaConnection,
        ]
        for tool in tools {
            for failure in FakeClient.Failure.allCases {
                var client = FakeClient()
                client.call(tool: tool, failure: failure)
                for _ in 0..<100 { client.statusTick() }
                XCTAssertEqual(client.sends, 1, tool)
                XCTAssertEqual(client.sessionRetries, 0, tool)
                XCTAssertEqual(client.pendingJobs, tool == ToolCatalog.prepareProviderRuntime ? 1 : 0, tool)
            }
        }
    }

    func testReadOnlyProviderDiscoveryKeepsSessionRetry() {
        for tool in [ToolCatalog.listProviders, ToolCatalog.listProviderConnections, ToolCatalog.getProviderAuthStatus] {
            var client = FakeClient()
            client.call(tool: tool, failure: .http404)
            XCTAssertEqual(client.sends, 2, tool)
            XCTAssertEqual(client.sessionRetries, 1, tool)
            XCTAssertEqual(client.pendingJobs, 0, tool)
        }
    }

    func testNextEmptyProbeAndDiscoveredLegacySessionNeverResendRefreshInput() {
        var client = FakeClient()
        client.call(tool: ToolCatalog.getServerInfo, failure: .http404, arguments: ["refresh_permissions": true])

        // A fresh session is unknown until its first empty probe returns the new version.
        let rediscovery = RecordingPermissionContract.serverInfoArguments(contractVersion: nil, refreshPermissions: true)
        client.call(tool: ToolCatalog.getServerInfo, arguments: rediscovery)
        XCTAssertEqual(client.sentArguments, [["refresh_permissions": true], [:]])

        let legacy = RecordingPermissionContract.serverInfoArguments(
            contractVersion: "1.0.0", serverInstanceID: "00000000-0000-4000-8000-000000000002",
            refreshPermissions: true)
        client.call(tool: ToolCatalog.getServerInfo, failure: .http404, arguments: legacy)
        XCTAssertEqual(client.sentArguments, [["refresh_permissions": true], [:], [:], [:]])
        XCTAssertEqual(client.sessionRetries, 1, "The ordinary empty probe keeps its existing session retry")
    }

    func testFakeStillExercisesOrdinarySessionAndJobRecovery() {
        var ordinary = FakeClient()
        ordinary.call(tool: ToolCatalog.getServerInfo, failure: .http404)
        XCTAssertEqual(ordinary.sends, 2)
        XCTAssertEqual(ordinary.sessionRetries, 1)
        XCTAssertEqual(ordinary.pendingJobs, 0)
        XCTAssertEqual(ordinary.sentArguments, [[:], [:]])

        var job = FakeClient()
        job.call(tool: ToolCatalog.regenerate, failure: .transport)
        XCTAssertEqual(job.sends, 1)
        XCTAssertEqual(job.pendingJobs, 1)
        job.statusTick()
        XCTAssertEqual(job.sends, 2)
        XCTAssertEqual(job.pendingJobs, 1)
    }

    func testAppUsesSharedPolicyAtBothAutomaticReplayEntryPoints() throws {
        let app = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
            .deletingLastPathComponent().deletingLastPathComponent()
        let client = try String(
            contentsOf: app.appendingPathComponent("Sources/NarumiMenuBar/MCPClient.swift"), encoding: .utf8)
        let recovery = try String(
            contentsOf: app.appendingPathComponent("Sources/NarumiMenuBar/MCPClient+JobRecovery.swift"), encoding: .utf8)
        XCTAssertTrue(client.contains("MCPToolReplayPolicy.allowsSessionRetry("))
        XCTAssertTrue(client.contains("refreshingPermissions:"))
        XCTAssertTrue(recovery.contains("MCPToolReplayPolicy.createsJob("))
    }

    /// Fake HTTP failures plus the real job ledger exercise both recovery entry points
    /// without a live server, OS dialog or elapsed-time wait.
    private struct FakeClient {
        enum Failure: CaseIterable, Equatable { case http404, transport, invalidResponse }

        private var jobs = DesktopJobRequestState()
        private(set) var sends = 0
        private(set) var sessionRetries = 0
        private(set) var sentArguments: [[String: Bool]] = []
        var pendingJobs: Int { jobs.pendingCount }

        mutating func call(tool: String, failure: Failure? = nil, arguments: [String: Bool] = [:]) {
            let request = DesktopJobRequestState.Request(
                requestID: "fixture-request", tool: tool, arguments: Data(#"{"request_id":"fixture-request"}"#.utf8))
            let token = MCPToolReplayPolicy.createsJob(tool: tool) ? jobs.begin(request) : nil
            sends += 1
            sentArguments.append(arguments)
            if failure == .http404, MCPToolReplayPolicy.allowsSessionRetry(
                tool: tool, refreshingPermissions: arguments["refresh_permissions"] == true)
            {
                sessionRetries += 1
                sends += 1
                sentArguments.append(arguments)
            }
            if let token {
                if failure == nil {
                    jobs.confirm(token)
                } else {
                    jobs.markUncertain(token)
                }
            }
        }

        mutating func statusTick() {
            guard let replay = jobs.beginRetry() else { return }
            sends += 1
            sentArguments.append([:])
            jobs.markUncertain(replay.token)
        }
    }
}
