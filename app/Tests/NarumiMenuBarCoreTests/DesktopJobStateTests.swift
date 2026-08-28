import XCTest

@testable import NarumiMenuBarCore

final class DesktopJobStateTests: XCTestCase {
    private func job(_ id: String, status: String) -> Job {
        Job(
            jobID: id, meetingID: nil, kind: "process", status: status,
            progress: nil, result: nil, error: nil,
            createdAt: "2026-08-28T00:00:00Z", updatedAt: "2026-08-28T00:00:00Z")
    }

    func testUnknownIDBlocksImmediatelyAndTrackingIsUnique() {
        var state = DesktopJobState()
        XCTAssertNil(state.beginRefresh())
        XCTAssertEqual(state.activeCount, 0)
        state.track(jobID: "first")
        state.track(jobID: "second")
        state.track(jobID: "first")
        XCTAssertEqual(state.trackedIDs, ["second", "first"])
        XCTAssertEqual(state.jobs, [])
        XCTAssertEqual(state.knownActiveIDs, ["first", "second"])
        XCTAssertEqual(state.activeCount, 2)
    }

    func testQueuedAndRunningJobsRemainActiveInTrackingOrder() throws {
        var state = DesktopJobState()
        state.track(jobID: "queued")
        state.track(jobID: "running")
        let token = try XCTUnwrap(state.beginRefresh())
        XCTAssertEqual(token.ids, ["running", "queued"])
        let queued = job("queued", status: "queued")
        let running = job("running", status: "running")
        XCTAssertTrue(state.finishRefresh(token, jobs: [queued, running]))
        XCTAssertEqual(state.jobs, [running, queued])
        XCTAssertEqual(state.knownActiveIDs, ["queued", "running"])
        XCTAssertEqual(state.activeCount, 2)
    }

    func testEveryTerminalStatusUnblocksUpdatesButKeepsHistory() throws {
        for status in ["succeeded", "failed", "cancelled"] {
            var state = DesktopJobState()
            state.track(jobID: "job")
            let token = try XCTUnwrap(state.beginRefresh())
            let finished = job("job", status: status)
            XCTAssertTrue(state.finishRefresh(token, jobs: [finished]))
            XCTAssertEqual(state.activeCount, 0, status)
            XCTAssertEqual(state.knownActiveIDs, [], status)
            XCTAssertEqual(state.trackedIDs, ["job"], status)
            XCTAssertEqual(state.jobs, [finished], status)
        }
    }

    func testUnknownStatusDoesNotUnblockUpdates() throws {
        var state = DesktopJobState()
        state.track(jobID: "job")
        let token = try XCTUnwrap(state.beginRefresh())
        XCTAssertTrue(state.finishRefresh(token, jobs: [job("job", status: "unrecognized")]))
        XCTAssertEqual(state.knownActiveIDs, ["job"])
        state.clearFinished()
        XCTAssertEqual(state.trackedIDs, ["job"])
    }

    func testTransportFailurePreservesKnownAndUnknownActivity() throws {
        var state = DesktopJobState()
        state.track(jobID: "running")
        let first = try XCTUnwrap(state.beginRefresh())
        let running = job("running", status: "running")
        XCTAssertTrue(state.finishRefresh(first, jobs: [running]))
        state.track(jobID: "unknown")
        let failed = try XCTUnwrap(state.beginRefresh())
        XCTAssertTrue(state.finishRefresh(failed, jobs: []))
        XCTAssertEqual(state.jobs, [running])
        XCTAssertEqual(state.trackedIDs, ["unknown", "running"])
        XCTAssertEqual(state.knownActiveIDs, ["unknown", "running"])
        XCTAssertEqual(state.activeCount, 2)
        XCTAssertNotNil(state.beginRefresh())
    }

    func testPartialFailurePreservesPreviousStatus() throws {
        var state = DesktopJobState()
        state.track(jobID: "failed-query")
        state.track(jobID: "finished")
        let first = try XCTUnwrap(state.beginRefresh())
        let running = job("failed-query", status: "running")
        state.finishRefresh(first, jobs: [running, job("finished", status: "queued")])
        let next = try XCTUnwrap(state.beginRefresh())
        let finished = job("finished", status: "succeeded")
        state.finishRefresh(next, jobs: [finished])
        XCTAssertEqual(state.jobs, [finished, running])
        XCTAssertEqual(state.knownActiveIDs, ["failed-query"])
    }

    func testConfirmedNotFoundRemovesKnownAndUnknownIDs() throws {
        var state = DesktopJobState()
        state.track(jobID: "known")
        let first = try XCTUnwrap(state.beginRefresh())
        state.finishRefresh(first, jobs: [job("known", status: "running")])
        state.track(jobID: "unknown")
        let token = try XCTUnwrap(state.beginRefresh())
        XCTAssertTrue(state.finishRefresh(token, jobs: [], missingIDs: ["known", "unknown"]))
        XCTAssertEqual(state.trackedIDs, [])
        XCTAssertEqual(state.jobs, [])
        XCTAssertEqual(state.activeCount, 0)
        XCTAssertNil(state.beginRefresh())
    }

    func testTrackInvalidatesOldPollWithoutRemovingNewID() throws {
        var state = DesktopJobState()
        state.track(jobID: "older")
        let stale = try XCTUnwrap(state.beginRefresh())
        state.track(jobID: "newer")
        let current = try XCTUnwrap(state.beginRefresh())
        let before = state
        XCTAssertFalse(state.finishRefresh(stale, jobs: [], missingIDs: ["older"]))
        XCTAssertEqual(state, before)
        XCTAssertEqual(state.knownActiveIDs, ["older", "newer"])
        XCTAssertTrue(state.finishRefresh(current, jobs: [job("older", status: "succeeded")]))
        XCTAssertEqual(state.trackedIDs, ["newer", "older"])
        XCTAssertEqual(state.knownActiveIDs, ["newer"])
    }

    func testConnectionInvalidationRejectsStalePollAndKeepsSingleFlight() throws {
        var state = DesktopJobState()
        state.track(jobID: "job")
        let stale = try XCTUnwrap(state.beginRefresh())
        state.invalidateRefresh()
        let current = try XCTUnwrap(state.beginRefresh())
        let before = state
        XCTAssertFalse(state.finishRefresh(stale, jobs: [job("job", status: "succeeded")]))
        XCTAssertEqual(state, before)
        XCTAssertNil(state.beginRefresh())
        XCTAssertTrue(state.finishRefresh(current, jobs: [job("job", status: "running")]))
        XCTAssertEqual(state.activeCount, 1)
    }

    func testClearFinishedPreservesUnknownIDsAndInvalidatesPoll() throws {
        var state = DesktopJobState()
        state.track(jobID: "finished")
        state.track(jobID: "running")
        state.track(jobID: "unknown")
        let first = try XCTUnwrap(state.beginRefresh())
        let running = job("running", status: "running")
        state.finishRefresh(first, jobs: [job("finished", status: "failed"), running])
        let stale = try XCTUnwrap(state.beginRefresh())
        state.clearFinished()
        XCTAssertEqual(state.trackedIDs, ["unknown", "running"])
        XCTAssertEqual(state.jobs, [running])
        XCTAssertEqual(state.knownActiveIDs, ["unknown", "running"])
        let before = state
        XCTAssertFalse(state.finishRefresh(stale, jobs: [], missingIDs: ["unknown", "running"]))
        XCTAssertEqual(state, before)
        XCTAssertNotNil(state.beginRefresh())
    }

    func testOnlyOneRefreshCanBeActiveAndConsumedTokenCannotBeReused() throws {
        var state = DesktopJobState()
        state.track(jobID: "job")
        let first = try XCTUnwrap(state.beginRefresh())
        XCTAssertNil(state.beginRefresh())
        XCTAssertTrue(state.finishRefresh(first, jobs: [job("job", status: "running")]))
        XCTAssertFalse(state.finishRefresh(first, jobs: [], missingIDs: ["job"]))
        let second = try XCTUnwrap(state.beginRefresh())
        XCTAssertNotEqual(first, second)
        XCTAssertFalse(state.finishRefresh(first, jobs: [], missingIDs: ["job"]))
        XCTAssertNil(state.beginRefresh())
        XCTAssertTrue(state.finishRefresh(second, jobs: [job("job", status: "succeeded")]))
        XCTAssertEqual(state.activeCount, 0)
    }

    func testUnrequestedResultsAreIgnoredAndReturnedStatusWinsOverMissing() throws {
        var state = DesktopJobState()
        state.track(jobID: "job")
        let token = try XCTUnwrap(state.beginRefresh())
        let running = job("job", status: "running")
        state.finishRefresh(
            token, jobs: [running, job("unrequested", status: "running")],
            missingIDs: ["job", "unrequested"])
        XCTAssertEqual(state.trackedIDs, ["job"])
        XCTAssertEqual(state.jobs, [running])
        XCTAssertEqual(state.knownActiveIDs, ["job"])
    }
}
