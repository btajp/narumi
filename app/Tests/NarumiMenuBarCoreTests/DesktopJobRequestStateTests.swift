import XCTest

@testable import NarumiMenuBarCore

final class DesktopJobRequestStateTests: XCTestCase {
    private func request(
        _ id: String, tool: String = ToolCatalog.stopRecording, runAsync: Bool? = nil
    ) -> DesktopJobRequestState.Request {
        let asyncArgument = runAsync.map { ", \"run_async\": \($0)" } ?? ""
        return .init(
            requestID: id, tool: tool, arguments: Data("{ \"request_id\": \"\(id)\"\(asyncArgument) }".utf8))
    }

    func testInitialSendCountsAsPendingButCannotBeRetried() throws {
        var state = DesktopJobRequestState()
        XCTAssertEqual(state.pendingCount, 0)
        XCTAssertEqual(state.uncertainCount, 0)
        XCTAssertEqual(state.pendingTools, [])
        XCTAssertNil(state.beginRetry())
        _ = try XCTUnwrap(state.begin(request("request-1")))
        XCTAssertEqual(state.pendingCount, 1)
        XCTAssertEqual(state.uncertainCount, 0)
        XCTAssertEqual(state.pendingTools, [ToolCatalog.stopRecording])
        XCTAssertNil(state.beginRetry())
    }

    func testRetryPreservesExactIDToolAndArgumentBytes() throws {
        var state = DesktopJobRequestState()
        let arguments = Data("{\n  \"request_id\": \"request-1\", \"auto_process\": true, \"discard_video\": false\n}".utf8)
        let original = DesktopJobRequestState.Request(
            requestID: "request-1", tool: ToolCatalog.stopRecording, arguments: arguments)
        let token = try XCTUnwrap(state.begin(original))
        XCTAssertTrue(state.markUncertain(token))
        let retry = try XCTUnwrap(state.beginRetry())
        XCTAssertEqual(retry.request, original)
        XCTAssertEqual(retry.request.requestID, "request-1")
        XCTAssertEqual(retry.request.tool, ToolCatalog.stopRecording)
        XCTAssertEqual(retry.request.arguments, arguments)
        XCTAssertNotEqual(retry.token, token)
    }

    func testOriginalAndRetryFailuresNeverDropTheUnknownRequest() throws {
        var state = DesktopJobRequestState()
        let original = request("request-1")
        let token = try XCTUnwrap(state.begin(original))
        XCTAssertTrue(state.markUncertain(token))
        XCTAssertEqual(state.pendingCount, 1)
        XCTAssertEqual(state.uncertainCount, 1)
        for _ in 0..<3 {
            let retry = try XCTUnwrap(state.beginRetry())
            XCTAssertEqual(retry.request, original)
            XCTAssertEqual(state.pendingCount, 1)
            XCTAssertEqual(state.uncertainCount, 0)
            XCTAssertTrue(state.markUncertain(retry.token))
            XCTAssertEqual(state.pendingCount, 1)
            XCTAssertEqual(state.uncertainCount, 1)
            XCTAssertEqual(state.pendingTools, [ToolCatalog.stopRecording])
        }
    }

    func testDuplicateIDCannotReplaceUnresolvedArgumentsOrStartAnotherAttempt() throws {
        var state = DesktopJobRequestState()
        let original = request("request-1")
        let changed = request("request-1", tool: ToolCatalog.regenerate)
        let token = try XCTUnwrap(state.begin(original))
        XCTAssertNil(state.begin(original))
        XCTAssertNil(state.begin(changed))
        XCTAssertTrue(state.markUncertain(token))
        XCTAssertNil(state.begin(changed))
        let retry = try XCTUnwrap(state.beginRetry())
        XCTAssertEqual(retry.request, original)
        XCTAssertNil(state.beginRetry())
        XCTAssertEqual(state.pendingCount, 1)
    }

    func testSuccessfulRetryRemovesUncertaintyAndRejectsDuplicateCompletion() throws {
        var state = DesktopJobRequestState()
        let initial = try XCTUnwrap(state.begin(request("request-1")))
        state.markUncertain(initial)
        let retry = try XCTUnwrap(state.beginRetry())
        XCTAssertTrue(state.confirm(retry.token))
        XCTAssertEqual(state.pendingCount, 0)
        XCTAssertEqual(state.uncertainCount, 0)
        XCTAssertEqual(state.pendingTools, [])
        XCTAssertNil(state.beginRetry())
        XCTAssertFalse(state.confirm(retry.token))
        XCTAssertFalse(state.markUncertain(retry.token))
    }

    func testConfirmedInitialRejectionRemovesPendingWithoutRetry() throws {
        var state = DesktopJobRequestState()
        let token = try XCTUnwrap(state.begin(request("rejected-request")))
        XCTAssertTrue(state.confirm(token))
        XCTAssertEqual(state.pendingCount, 0)
        XCTAssertEqual(state.uncertainCount, 0)
        XCTAssertNil(state.beginRetry())
    }

    private let rejectionCases: [(tool: String, preflightCodes: Set<String>)] = [
        (ToolCatalog.regenerate,
            ["invalid_argument", "not_found", "busy", "scope_denied", "policy_violation", "engine_unavailable"]),
        (ToolCatalog.importRecording,
            ["invalid_argument", "not_found", "busy", "policy_violation", "engine_unavailable"]),
        (ToolCatalog.registerContext, ["invalid_argument", "not_found", "busy", "scope_denied"]),
        (ToolCatalog.exportMinutes, ["invalid_argument", "not_found", "busy", "scope_denied"]),
        (ToolCatalog.stopRecording, ["invalid_argument", "not_found", "busy"]),
        ("unknown_tool", []),
    ]
    private let failureCodes: [String?] = [
        "invalid_argument", "not_found", "busy", "scope_denied", "policy_violation", "engine_unavailable",
        "contract_mismatch", "cancelled", "recorder_unavailable", "internal",
        "permission_denied", "scope_mismatch", "unknown_code", nil,
    ]

    func testFirstFailureUsesTheToolSpecificContractPreflightTable() throws {
        for row in rejectionCases {
            for code in failureCodes {
                var state = DesktopJobRequestState()
                let token = try XCTUnwrap(state.begin(request("request-1", tool: row.tool, runAsync: true)))
                let definitive = row.preflightCodes.contains(code ?? "")
                let label = "\(row.tool): \(code ?? "no structured error")"
                XCTAssertTrue(state.finishFailure(token, errorCode: code), label)
                XCTAssertEqual(state.pendingCount, definitive ? 0 : 1, label)
                XCTAssertEqual(state.uncertainCount, definitive ? 0 : 1, label)
                if definitive {
                    XCTAssertNil(state.beginRetry(), label)
                } else {
                    XCTAssertEqual(try XCTUnwrap(state.beginRetry()).request.tool, row.tool, label)
                }
            }
        }
    }

    func testEveryReplayErrorRetainsTheOriginalUnknownRequest() throws {
        for row in rejectionCases {
            for code in failureCodes {
                var state = DesktopJobRequestState()
                let original = request("request-1", tool: row.tool, runAsync: true)
                let send = try XCTUnwrap(state.begin(original))
                XCTAssertTrue(state.finishFailure(send, errorCode: nil))
                let retry = try XCTUnwrap(state.beginRetry())
                let label = "\(row.tool): \(code ?? "no structured error")"
                XCTAssertTrue(state.finishFailure(retry.token, errorCode: code), label)
                XCTAssertEqual(state.pendingCount, 1, label)
                XCTAssertEqual(state.uncertainCount, 1, label)
                XCTAssertEqual(try XCTUnwrap(state.beginRetry()).request, original, label)
            }
        }
    }

    func testDefinitiveRegenerateRejectionsReleaseRecordingAndUpdateGates() throws {
        for code in ["scope_denied", "policy_violation", "engine_unavailable"] {
            var requests = DesktopJobRequestState()
            var session = DesktopSessionState()
            session.connectionChanged(to: .running(pid: 123))
            let poll = try XCTUnwrap(session.beginPoll())
            session.finishPoll(
                poll, info: ServerInfoSummary(recordingCapable: true), recording: .init(active: false))
            let send = try XCTUnwrap(requests.begin(request("request-1", tool: ToolCatalog.regenerate)))
            session.setJobRequestState(pending: true, pendingStop: false)
            XCTAssertFalse(session.canStart)
            XCTAssertNotNil(session.updateBlockReason(launcherBusy: false, knownJobsBusy: false))

            XCTAssertTrue(requests.finishFailure(send, errorCode: code))
            session.setJobRequestState(pending: requests.pendingCount > 0, pendingStop: false)
            XCTAssertTrue(session.canStart, code)
            XCTAssertNil(session.updateBlockReason(launcherBusy: false, knownJobsBusy: false), code)
        }
    }

    func testStaleFailureCannotClearANewerRetry() throws {
        var state = DesktopJobRequestState()
        let initial = try XCTUnwrap(state.begin(request("request-1", tool: ToolCatalog.regenerate)))
        XCTAssertTrue(state.finishFailure(initial, errorCode: nil))
        let retry = try XCTUnwrap(state.beginRetry())
        let before = state
        XCTAssertFalse(state.finishFailure(initial, errorCode: "scope_denied"))
        XCTAssertEqual(state, before)
        XCTAssertTrue(state.finishFailure(retry.token, errorCode: "scope_denied"))
        XCTAssertEqual(state.pendingCount, 1)
        XCTAssertEqual(state.uncertainCount, 1)
    }

    func testUnknownSynchronousExportIsHeldWithoutAutomaticReplay() throws {
        let asyncModes: [Bool?] = [nil, false]
        for runAsync in asyncModes {
            for code in [nil, "engine_unavailable", "internal", "contract_mismatch"] {
                var state = DesktopJobRequestState()
                let original = request("export-1", tool: ToolCatalog.exportMinutes, runAsync: runAsync)
                XCTAssertFalse(original.canAutomaticallyRetry)
                let send = try XCTUnwrap(state.begin(original))
                XCTAssertFalse(state.requiresManualRecovery(requestID: "export-1"))
                XCTAssertTrue(state.finishFailure(send, errorCode: code))
                XCTAssertTrue(state.requiresManualRecovery(requestID: "export-1"))
                for _ in 0..<3 { XCTAssertNil(state.beginRetry()) }
                XCTAssertEqual(state.pendingCount, 1)
                XCTAssertEqual(state.uncertainCount, 1)
            }
        }
    }

    func testUnknownSynchronousExportDoesNotStarveOtherJobRecovery() throws {
        var state = DesktopJobRequestState()
        let export = try XCTUnwrap(state.begin(request("export-1", tool: ToolCatalog.exportMinutes)))
        let regenerate = try XCTUnwrap(state.begin(request("regenerate-1", tool: ToolCatalog.regenerate)))
        state.finishFailure(export, errorCode: "engine_unavailable")
        state.finishFailure(regenerate, errorCode: nil)
        let retry = try XCTUnwrap(state.beginRetry())
        XCTAssertEqual(retry.request.requestID, "regenerate-1")
        XCTAssertTrue(state.confirm(retry.token))
        XCTAssertTrue(state.requiresManualRecovery(requestID: "export-1"))
        XCTAssertEqual(state.pendingCount, 1)
        XCTAssertNil(state.beginRetry())
    }

    func testOnlyAnExplicitBooleanAsyncExportCanAutomaticallyReplay() {
        let cases: [(String, Bool)] = [
            (#"{"run_async":true}"#, true),
            (#"{"run_async":false}"#, false),
            (#"{"run_async":null}"#, false),
            (#"{"run_async":"true"}"#, false),
            (#"{"run_async":1}"#, false),
            ("{}", false),
            ("invalid JSON", false),
        ]
        for (arguments, expected) in cases {
            let request = DesktopJobRequestState.Request(
                requestID: "export-1", tool: ToolCatalog.exportMinutes, arguments: Data(arguments.utf8))
            XCTAssertEqual(request.canAutomaticallyRetry, expected, arguments)
        }
    }

    func testStaleCallbackCannotRemoveOrResetANewerRetry() throws {
        var state = DesktopJobRequestState()
        let initial = try XCTUnwrap(state.begin(request("request-1")))
        state.markUncertain(initial)
        let first = try XCTUnwrap(state.beginRetry())
        state.markUncertain(first.token)
        let current = try XCTUnwrap(state.beginRetry())
        let before = state
        XCTAssertFalse(state.confirm(initial))
        XCTAssertFalse(state.confirm(first.token))
        XCTAssertFalse(state.markUncertain(first.token))
        XCTAssertEqual(state, before)
        XCTAssertNil(state.beginRetry())
        XCTAssertTrue(state.confirm(current.token))
    }

    func testMultiplePendingRequestsRetryInRegistrationOrderWithoutDuplicatingInflight() throws {
        var state = DesktopJobRequestState()
        let firstRequest = request("first", tool: ToolCatalog.regenerate)
        let secondRequest = request("second")
        let first = try XCTUnwrap(state.begin(firstRequest))
        let second = try XCTUnwrap(state.begin(secondRequest))
        state.markUncertain(second)
        state.markUncertain(first)
        XCTAssertEqual(state.pendingCount, 2)
        XCTAssertEqual(state.uncertainCount, 2)
        XCTAssertEqual(state.pendingTools, [ToolCatalog.regenerate, ToolCatalog.stopRecording])
        let retryFirst = try XCTUnwrap(state.beginRetry())
        XCTAssertEqual(retryFirst.request, firstRequest)
        let retrySecond = try XCTUnwrap(state.beginRetry())
        XCTAssertEqual(retrySecond.request, secondRequest)
        XCTAssertNil(state.beginRetry())
        XCTAssertEqual(state.pendingCount, 2)
        XCTAssertEqual(state.uncertainCount, 0)
        state.confirm(retryFirst.token)
        XCTAssertEqual(state.pendingCount, 1)
        XCTAssertEqual(state.pendingTools, [ToolCatalog.stopRecording])
        state.confirm(retrySecond.token)
        XCTAssertEqual(state.pendingCount, 0)
    }

    func testInvalidatingRetriesRetainsOriginalSendsAndUnknownRequests() throws {
        var state = DesktopJobRequestState()
        let originalSend = try XCTUnwrap(state.begin(request("initial", tool: ToolCatalog.regenerate)))
        let lostSend = try XCTUnwrap(state.begin(request("lost")))
        state.markUncertain(lostSend)
        let staleRetry = try XCTUnwrap(state.beginRetry())
        state.invalidateRetries()
        XCTAssertEqual(state.pendingCount, 2)
        XCTAssertEqual(state.uncertainCount, 1)
        XCTAssertFalse(state.confirm(staleRetry.token))
        XCTAssertFalse(state.markUncertain(staleRetry.token))
        XCTAssertTrue(state.confirm(originalSend))
        let current = try XCTUnwrap(state.beginRetry())
        XCTAssertEqual(current.request, staleRetry.request)
        XCTAssertNotEqual(current.token, staleRetry.token)
        XCTAssertTrue(state.confirm(current.token))
        XCTAssertEqual(state.pendingCount, 0)
    }

    func testPreviouslyConfirmedTokenCannotResolveNewRegistrationWithSameID() throws {
        var state = DesktopJobRequestState()
        let original = request("request-1")
        let old = try XCTUnwrap(state.begin(original))
        state.confirm(old)
        let current = try XCTUnwrap(state.begin(original))
        let before = state
        XCTAssertNotEqual(old, current)
        XCTAssertFalse(state.confirm(old))
        XCTAssertFalse(state.markUncertain(old))
        XCTAssertEqual(state, before)
        XCTAssertTrue(state.confirm(current))
    }

    func testLostStopResponseBlocksUpdatesUntilRecoveredJobReachesTerminalState() throws {
        var session = DesktopSessionState()
        var requests = DesktopJobRequestState()
        var jobs = DesktopJobState()
        let info = ServerInfoSummary(recordingCapable: true)
        session.connectionChanged(to: .running(pid: 123))
        let activePoll = try XCTUnwrap(session.beginPoll())
        session.finishPoll(activePoll, info: info, recording: .init(active: true, meetingID: "meeting-1"))
        let stop = try XCTUnwrap(session.beginStop())
        let original = request("stop-request-1")
        let send = try XCTUnwrap(requests.begin(original))

        // The server stopped recording and queued work, but the response was lost.
        XCTAssertTrue(requests.markUncertain(send))
        XCTAssertTrue(session.failOperation(stop))
        let inactivePoll = try XCTUnwrap(session.beginPoll())
        session.finishPoll(inactivePoll, info: info, recording: .init(active: false))
        jobs.clearFinished()
        XCTAssertFalse(session.recording.active)
        XCTAssertEqual(jobs.activeCount, 0)
        XCTAssertEqual(requests.pendingCount, 1)
        XCTAssertNotNil(session.updateBlockReason(
            launcherBusy: false, knownJobsBusy: jobs.activeCount > 0 || requests.pendingCount > 0))

        let retry = try XCTUnwrap(requests.beginRetry())
        XCTAssertEqual(retry.request, original)
        XCTAssertNotNil(session.updateBlockReason(
            launcherBusy: false, knownJobsBusy: jobs.activeCount > 0 || requests.pendingCount > 0))
        // Track the recovered job before removing the unknown-request blocker.
        jobs.track(jobID: "job-recovered")
        XCTAssertTrue(requests.confirm(retry.token))
        XCTAssertEqual(requests.pendingCount, 0)
        XCTAssertNotNil(session.updateBlockReason(
            launcherBusy: false, knownJobsBusy: jobs.activeCount > 0 || requests.pendingCount > 0))

        let poll = try XCTUnwrap(jobs.beginRefresh())
        let finished = Job(
            jobID: "job-recovered", meetingID: "meeting-1", kind: "process", status: "succeeded",
            progress: nil, result: nil, error: nil,
            createdAt: "2026-08-28T00:00:00Z", updatedAt: "2026-08-28T00:01:00Z")
        XCTAssertTrue(jobs.finishRefresh(poll, jobs: [finished]))
        XCTAssertEqual(jobs.activeCount, 0)
        XCTAssertNil(session.updateBlockReason(
            launcherBusy: false, knownJobsBusy: jobs.activeCount > 0 || requests.pendingCount > 0))
    }
}
