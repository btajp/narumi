import XCTest

@testable import NarumiMenuBarCore

final class DesktopJobRequestStateTests: XCTestCase {
    private func request(
        _ id: String, tool: String = ToolCatalog.stopRecording
    ) -> DesktopJobRequestState.Request {
        .init(requestID: id, tool: tool, arguments: Data(#"{ "request_id": "\#(id)" }"#.utf8))
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
