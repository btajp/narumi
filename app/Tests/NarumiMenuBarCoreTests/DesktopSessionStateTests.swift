import XCTest

@testable import NarumiMenuBarCore

final class DesktopSessionStateTests: XCTestCase {
    private let info = ServerInfoSummary(version: "0.1.1", recordingCapable: true)

    private func ready(recording: RecordingStatus = .init(active: false)) throws -> DesktopSessionState {
        var state = DesktopSessionState()
        state.connectionChanged(to: .running(pid: 123))
        let poll = try XCTUnwrap(state.beginPoll())
        XCTAssertTrue(state.finishPoll(poll, info: info, recording: recording))
        return state
    }

    func testStartRequiresReadyCapabilityAndConfirmedIdleRecording() throws {
        var state = DesktopSessionState()
        XCTAssertFalse(state.canStart)
        XCTAssertFalse(state.canStop)
        state.connectionChanged(to: .preparing(step: "依存を準備"))
        XCTAssertNil(state.beginPoll())
        XCTAssertFalse(state.canStart)
        state.connectionChanged(to: .running(pid: 123))
        XCTAssertFalse(state.canStart)
        let poll = try XCTUnwrap(state.beginPoll())
        state.finishPoll(poll, info: info, recording: .init(active: false))
        XCTAssertTrue(state.canStart)
        XCTAssertEqual(state.statusText, "録画を開始できます")
    }

    func testUnavailableOrUnknownCapabilityCannotStart() throws {
        for capability in [false, nil] as [Bool?] {
            var state = try ready()
            let poll = try XCTUnwrap(state.beginPoll())
            state.finishPoll(poll, info: .init(recordingCapable: capability), recording: .init(active: false))
            XCTAssertFalse(state.canStart)
            XCTAssertTrue(state.statusText.contains("診断"))
        }
    }

    func testLateIdlePollCannotOverwriteSuccessfulStart() throws {
        var state = try ready()
        let stalePoll = try XCTUnwrap(state.beginPoll())
        let start = try XCTUnwrap(state.beginStart())
        XCTAssertFalse(state.canStart)
        XCTAssertNil(state.beginPoll())
        XCTAssertFalse(state.finishPoll(stalePoll, info: info, recording: .init(active: false)))
        XCTAssertTrue(state.finishStart(start, recording: .init(active: true, meetingID: "meeting-1", elapsedSec: 0)))
        XCTAssertFalse(state.finishPoll(stalePoll, info: info, recording: .init(active: false)))
        XCTAssertTrue(state.recording.active)
        XCTAssertTrue(state.canStop)
    }

    func testLateActivePollCannotOverwriteSuccessfulStop() throws {
        var state = try ready(recording: .init(active: true, elapsedSec: 31))
        let stalePoll = try XCTUnwrap(state.beginPoll())
        let stop = try XCTUnwrap(state.beginStop())
        XCTAssertFalse(state.canStop)
        XCTAssertTrue(state.finishStop(stop))
        XCTAssertFalse(state.finishPoll(stalePoll, info: info, recording: .init(active: true, elapsedSec: 32)))
        XCTAssertFalse(state.recording.active)
        XCTAssertTrue(state.canStart)
    }

    func testCancelledStartDialogRestoresAvailabilityWithoutSendingRequest() throws {
        var state = try ready()
        let start = try XCTUnwrap(state.beginStart())
        XCTAssertTrue(state.cancelStart(start))
        XCTAssertTrue(state.canStart)
        XCTAssertNil(state.operation)
        XCTAssertTrue(state.recordingIsConfirmed)
        XCTAssertFalse(state.finishStart(start, recording: .init(active: true)))
    }

    func testFailedPollPreservesRecordingAndMarksUncertainty() throws {
        let recording = RecordingStatus(active: true, meetingID: "meeting-1", elapsedSec: 90)
        var state = try ready(recording: recording)
        let poll = try XCTUnwrap(state.beginPoll())
        XCTAssertTrue(state.failPoll(poll))
        XCTAssertEqual(state.recording, recording)
        XCTAssertFalse(state.recordingIsConfirmed)
        XCTAssertFalse(state.canStart)
        XCTAssertFalse(state.canStop)
        XCTAssertTrue(state.statusText.contains("録画中"))
        XCTAssertNotNil(state.updateBlockReason(launcherBusy: false, knownJobsBusy: false))
    }

    func testFailedStopKeepsActiveRecordingUntilCurrentPollConfirmsStop() throws {
        var state = try ready(recording: .init(active: true))
        let stop = try XCTUnwrap(state.beginStop())
        XCTAssertTrue(state.failOperation(stop))
        XCTAssertTrue(state.recording.active)
        XCTAssertFalse(state.recordingIsConfirmed)
        let poll = try XCTUnwrap(state.beginPoll())
        state.finishPoll(poll, info: info, recording: .init(active: false))
        XCTAssertFalse(state.recording.active)
        XCTAssertTrue(state.canStart)
    }

    func testFailedStartDoesNotAssumeItNeverReachedServer() throws {
        var state = try ready()
        let start = try XCTUnwrap(state.beginStart())
        XCTAssertTrue(state.failOperation(start))
        XCTAssertFalse(state.canStart)
        XCTAssertNotNil(state.updateBlockReason(launcherBusy: false, knownJobsBusy: false))
        let poll = try XCTUnwrap(state.beginPoll())
        state.finishPoll(poll, info: info, recording: .init(active: true, meetingID: "accepted-remotely"))
        XCTAssertTrue(state.recording.active)
        XCTAssertTrue(state.canStop)
    }

    func testConnectionChangeDiscardsOldPollAndOperationResults() throws {
        var state = try ready()
        let stalePoll = try XCTUnwrap(state.beginPoll())
        let staleStart = try XCTUnwrap(state.beginStart())
        XCTAssertTrue(state.isCurrentOperation(staleStart))
        let previousGeneration = state.connectionGeneration
        state.connectionChanged(to: .running(pid: 456))
        XCTAssertFalse(state.isCurrentOperation(staleStart))
        XCTAssertGreaterThan(state.connectionGeneration, previousGeneration)
        XCTAssertNil(state.operation)
        XCTAssertFalse(state.finishPoll(stalePoll, info: info, recording: .init(active: false)))
        XCTAssertFalse(state.finishStart(staleStart, recording: .init(active: true)))
        XCTAssertFalse(state.failOperation(staleStart))
        XCTAssertFalse(state.canStart)
        let poll = try XCTUnwrap(state.beginPoll())
        state.finishPoll(poll, info: info, recording: .init(active: true, meetingID: "new-server"))
        XCTAssertEqual(state.recording.meetingID, "new-server")
    }

    func testReconnectKeepsActiveIndicatorUntilConfirmed() throws {
        var state = try ready(recording: .init(active: true, meetingName: "会議"))
        state.connectionChanged(to: .failed("connection lost"))
        XCTAssertTrue(state.recording.active)
        XCTAssertFalse(state.recordingIsConfirmed)
        XCTAssertEqual(state.menuSymbolName, "record.circle.fill")
        XCTAssertTrue(state.accessibilityLabel.contains("再確認"))
    }

    func testPollsAreSingleFlightAndCompletedTokensCannotBeReused() throws {
        var state = try ready()
        let poll = try XCTUnwrap(state.beginPoll())
        XCTAssertNil(state.beginPoll())
        XCTAssertTrue(state.finishPoll(poll, info: info, recording: .init(active: false)))
        XCTAssertFalse(state.finishPoll(poll, info: info, recording: .init(active: true)))
        let newer = try XCTUnwrap(state.beginPoll())
        XCTAssertNotEqual(newer, poll)
        XCTAssertFalse(state.failPoll(poll))
        XCTAssertTrue(state.isCurrentPoll(newer))
    }

    func testUpdatePolicyDefersPreparingRecordingOperationsUnknownStatusAndJobs() throws {
        var state = try ready()
        XCTAssertNil(state.updateBlockReason(launcherBusy: false, knownJobsBusy: false))
        XCTAssertNotNil(state.updateBlockReason(launcherBusy: true, knownJobsBusy: false))
        XCTAssertNotNil(state.updateBlockReason(launcherBusy: false, knownJobsBusy: true))
        let start = try XCTUnwrap(state.beginStart())
        XCTAssertNotNil(state.updateBlockReason(launcherBusy: false, knownJobsBusy: false))
        state.finishStart(start, recording: .init(active: true))
        XCTAssertNotNil(state.updateBlockReason(launcherBusy: false, knownJobsBusy: false))
        _ = try XCTUnwrap(state.beginStop())
        XCTAssertNotNil(state.updateBlockReason(launcherBusy: false, knownJobsBusy: false))
        state.connectionChanged(to: .preparing(step: "準備"))
        XCTAssertNotNil(state.updateBlockReason(launcherBusy: false, knownJobsBusy: false))
        state.connectionChanged(to: .running(pid: 123))
        XCTAssertNotNil(state.updateBlockReason(launcherBusy: false, knownJobsBusy: false))
    }

    func testInstallLockPreventsNewStartsWithoutBlockingItsOwnQuit() throws {
        var state = try ready()
        state.setInstallingUpdate(true)
        XCTAssertFalse(state.canStart)
        XCTAssertNil(state.beginStart())
        XCTAssertNil(state.updateBlockReason(launcherBusy: false, knownJobsBusy: false))
        XCTAssertTrue(state.statusText.contains("アップデート"))
        state.setInstallingUpdate(false)
        XCTAssertTrue(state.canStart)
    }

    func testTerminationRejectsLatePollAndNewActions() throws {
        var state = try ready()
        let poll = try XCTUnwrap(state.beginPoll())
        state.beginTermination()
        XCTAssertFalse(state.canStart)
        XCTAssertFalse(state.canStop)
        XCTAssertNil(state.beginPoll())
        XCTAssertFalse(state.finishPoll(poll, info: info, recording: .init(active: false)))
        XCTAssertNotNil(state.updateBlockReason(launcherBusy: false, knownJobsBusy: false))
    }

    func testUnknownJobRequestBlocksUpdateAfterRecordingPollReportsInactive() throws {
        var state = try ready(recording: .init(active: true))
        state.setJobRequestState(pending: true, pendingStop: true)
        let poll = try XCTUnwrap(state.beginPoll())
        state.finishPoll(poll, info: info, recording: .init(active: false))
        XCTAssertFalse(state.canStart)
        XCTAssertNotNil(state.updateBlockReason(launcherBusy: false, knownJobsBusy: false))
        state.setJobRequestState(pending: false, pendingStop: false)
        XCTAssertTrue(state.canStart)
    }

    func testUnknownOtherJobDoesNotPreventStoppingCurrentRecording() throws {
        var state = try ready(recording: .init(active: true))
        state.setJobRequestState(pending: true, pendingStop: false)
        XCTAssertTrue(state.canStop)
        state.setJobRequestState(pending: true, pendingStop: true)
        XCTAssertFalse(state.canStop)
    }

    func testUserQuitRemainsAvailableDuringDeferredUpdateWithoutCycleCallback() throws {
        let state = try ready(recording: .init(active: true))
        XCTAssertTrue(state.shouldDeferUpdateTermination(
            updateOwnsTermination: true, updateInstalling: false, userRequestedQuit: false,
            launcherBusy: false, knownJobsBusy: false))
        XCTAssertFalse(state.shouldDeferUpdateTermination(
            updateOwnsTermination: true, updateInstalling: false, userRequestedQuit: true,
            launcherBusy: false, knownJobsBusy: false))
        // A later unprompted updater Quit still must not enter the recording-stop flow.
        XCTAssertTrue(state.shouldDeferUpdateTermination(
            updateOwnsTermination: true, updateInstalling: true, userRequestedQuit: false,
            launcherBusy: false, knownJobsBusy: false))
    }
}
