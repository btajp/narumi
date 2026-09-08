import XCTest

@testable import NarumiMenuBarCore

final class DesktopSessionFinalizationTests: XCTestCase {
    private let info = ServerInfoSummary(version: "0.1.1", recordingCapable: true)

    private func interrupted(_ meetingID: String = "meeting-1") -> RecordingStatus {
        RecordingStatus(active: true, meetingID: meetingID, recorderAlive: false)
    }

    private func ready(recording: RecordingStatus? = nil) throws -> DesktopSessionState {
        var state = DesktopSessionState()
        state.connectionChanged(to: .running(pid: 123))
        let poll = try XCTUnwrap(state.beginPoll())
        XCTAssertTrue(state.finishPoll(poll, info: info, recording: recording ?? interrupted()))
        return state
    }

    private func poll(_ state: inout DesktopSessionState, recording: RecordingStatus? = nil) throws {
        let token = try XCTUnwrap(state.beginPoll())
        XCTAssertTrue(state.finishPoll(token, info: info, recording: recording ?? interrupted()))
    }

    func testEndedRecorderWaitsForFinalizationBeforeAnotherRecordingOrUpdate() throws {
        let state = try ready()
        XCTAssertTrue(state.recordingNeedsFinalization)
        XCTAssertTrue(state.shouldFinalizeInterruptedRecording)
        XCTAssertTrue(state.canStop)
        XCTAssertFalse(state.canStart)
        XCTAssertEqual(state.statusText, "録画は終了しました。保存を待っています")
        XCTAssertEqual(state.menuSymbolName, "square.and.arrow.down")
        XCTAssertEqual(state.updateBlockReason(launcherBusy: false, knownJobsBusy: false), "録画の保存が確定するまで更新を延期します")
    }

    func testLiveOrLegacyRecordingDoesNotFinalizeAutomatically() throws {
        for alive in [true, nil] as [Bool?] {
            let state = try ready(recording: .init(active: true, recorderAlive: alive))
            XCTAssertFalse(state.recordingNeedsFinalization)
            XCTAssertFalse(state.shouldFinalizeInterruptedRecording)
            XCTAssertTrue(state.canStop)
            XCTAssertEqual(state.statusText, "録画中")
            XCTAssertEqual(state.menuSymbolName, "record.circle.fill")
        }
        let idle = try ready(recording: .init(active: false, recorderAlive: false))
        XCTAssertFalse(idle.recordingNeedsFinalization)
        XCTAssertFalse(idle.shouldFinalizeInterruptedRecording)
        XCTAssertTrue(idle.canStart)
    }

    func testFinalizationRunsOnlyOnceAndFailedRequestAllowsManualRetry() throws {
        var state = try ready()
        let stop = try XCTUnwrap(state.beginStop())
        XCTAssertFalse(state.shouldFinalizeInterruptedRecording)
        XCTAssertNil(state.beginStop())
        XCTAssertEqual(state.statusText, "録画を保存しています…")
        XCTAssertTrue(state.failOperation(stop))
        XCTAssertFalse(state.shouldFinalizeInterruptedRecording)
        XCTAssertEqual(state.statusText, "録画は終了しました。保存結果を確認中です")
        for _ in 0..<3 {
            try poll(&state)
            XCTAssertFalse(state.shouldFinalizeInterruptedRecording)
            XCTAssertFalse(state.canStart)
            XCTAssertTrue(state.canStop)
        }
        let retry = try XCTUnwrap(state.beginStop())
        XCTAssertTrue(state.finishStop(retry))
        XCTAssertFalse(state.recordingNeedsFinalization)
        XCTAssertTrue(state.canStart)
        XCTAssertEqual(state.menuSymbolName, "waveform")
    }

    func testFailedPollCannotTriggerFinalizationUntilStatusIsConfirmed() throws {
        var state = try ready()
        let token = try XCTUnwrap(state.beginPoll())
        XCTAssertTrue(state.failPoll(token))
        XCTAssertTrue(state.recordingNeedsFinalization)
        XCTAssertFalse(state.shouldFinalizeInterruptedRecording)
        XCTAssertFalse(state.canStop)
        XCTAssertEqual(state.statusText, "録画は終了しました。保存結果を確認中です")
        try poll(&state)
        XCTAssertTrue(state.shouldFinalizeInterruptedRecording)
    }

    func testReconnectPreservesAttemptForTheSameMeeting() throws {
        var state = try ready()
        let stop = try XCTUnwrap(state.beginStop())
        state.connectionChanged(to: .failed("connection lost"))
        XCTAssertFalse(state.shouldFinalizeInterruptedRecording)
        XCTAssertEqual(state.menuSymbolName, "square.and.arrow.down")
        XCTAssertEqual(state.statusText, "録画は終了しました。保存結果を確認中です")
        XCTAssertFalse(state.finishStop(stop))
        state.connectionChanged(to: .running(pid: 456))
        try poll(&state)
        XCTAssertFalse(state.shouldFinalizeInterruptedRecording)
        XCTAssertTrue(state.canStop)
    }

    func testNewMeetingResetsAutomaticFinalizationAttempt() throws {
        var state = try ready()
        let stop = try XCTUnwrap(state.beginStop())
        XCTAssertTrue(state.failOperation(stop))
        try poll(&state, recording: interrupted("meeting-2"))
        XCTAssertTrue(state.shouldFinalizeInterruptedRecording)
        let secondStop = try XCTUnwrap(state.beginStop())
        XCTAssertFalse(state.shouldFinalizeInterruptedRecording)
        XCTAssertTrue(state.finishStop(secondStop))
        XCTAssertTrue(state.canStart)
    }

    func testConfirmedIdleClearsPriorAttemptEvenWhenMeetingIDIsReused() throws {
        var state = try ready()
        let stop = try XCTUnwrap(state.beginStop())
        XCTAssertTrue(state.failOperation(stop))
        try poll(&state, recording: .init(active: false))
        XCTAssertTrue(state.canStart)
        try poll(&state)
        XCTAssertTrue(state.shouldFinalizeInterruptedRecording)
    }

    func testSuccessfulStartAndStopKeepFinalizationAvailableForNextRecording() throws {
        var state = try ready()
        let stop = try XCTUnwrap(state.beginStop())
        XCTAssertTrue(state.finishStop(stop))
        let start = try XCTUnwrap(state.beginStart())
        XCTAssertTrue(state.finishStart(start, recording: .init(active: true, meetingID: "meeting-2", recorderAlive: true)))
        XCTAssertFalse(state.shouldFinalizeInterruptedRecording)
        try poll(&state, recording: interrupted("meeting-2"))
        XCTAssertTrue(state.shouldFinalizeInterruptedRecording)
    }

    func testPendingStopInstallAndTerminationBlockAutomaticFinalization() throws {
        var state = try ready()
        state.setJobRequestState(pending: true, pendingStop: true)
        XCTAssertFalse(state.shouldFinalizeInterruptedRecording)
        XCTAssertNil(state.beginStop())
        XCTAssertEqual(state.statusText, "録画は終了しました。保存結果を確認中です")
        state.setJobRequestState(pending: false, pendingStop: false)
        XCTAssertTrue(state.shouldFinalizeInterruptedRecording)
        state.setInstallingUpdate(true)
        XCTAssertFalse(state.shouldFinalizeInterruptedRecording)
        XCTAssertNil(state.beginStop())
        state.setInstallingUpdate(false)
        XCTAssertTrue(state.shouldFinalizeInterruptedRecording)
        state.beginTermination()
        XCTAssertFalse(state.shouldFinalizeInterruptedRecording)
        XCTAssertNil(state.beginStop())
    }
}
