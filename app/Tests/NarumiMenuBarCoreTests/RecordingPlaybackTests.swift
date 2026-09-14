import XCTest

@testable import NarumiMenuBarCore

final class RecordingPlaybackTests: XCTestCase {
    private func detail() throws -> MeetingDetail {
        try JSONDecoder().decode(MeetingDetail.self, from: Data(#"""
            {
              "meeting":{"meeting_id":"20260827T030500Z-a1b2c3d4","meeting_name":"会議",
                "status":"recorded","started_at":"2026-08-27T03:05:00Z"},
              "bundle_path":"/recordings/meeting", "config":{},
              "recording":{"started_at":"2026-08-27T03:05:00Z","stopped_at":"2026-08-27T04:05:00Z",
                "duration_sec":3600,"tracks":{
                  "screen":{"path":"tracks/screen.mp4","bytes":3000,"discarded":false},
                  "mic":{"path":"tracks/mic.m4a","bytes":1000,"discarded":false},
                  "system":{"path":"tracks/system.m4a","bytes":1000,"discarded":false}}},
              "contexts":[],"minutes_versions":[],"latest_minutes":null,"exports":[],"artifacts":[]
            }
            """#.utf8))
    }

    private var playback: RecordingPlayback {
        RecordingPlayback(path: "/recordings/meeting/playback.mp4", sha256: String(repeating: "a", count: 64), bytes: 4500)
    }

    func testFileActionSelectsTheExistingMP4Itself() throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let file = directory.appendingPathComponent("recording.mp4")
        try Data([1, 2, 3]).write(to: file)

        XCTAssertEqual(RecordingPlayback.availableFileURL(at: file.path), file)
        XCTAssertNil(RecordingPlayback.availableFileURL(at: directory.path), "Never select a folder in place of the MP4")
        XCTAssertNil(RecordingPlayback.availableFileURL(at: "recording.mp4"), "Do not resolve a relative path against the app directory")

        try FileManager.default.removeItem(at: file)
        XCTAssertNil(RecordingPlayback.availableFileURL(at: file.path), "Recheck the file when an action is invoked")
    }

    func testExistingRecordingWithoutMinutesCanPreparePlayback() throws {
        let detail = try detail()
        XCTAssertNil(detail.playback)
        XCTAssertNil(detail.recording.display)
        XCTAssertNil(detail.recording.recorderError)
        let presentation = RecordingPlaybackPresentation(detail: detail, playbackFileExists: false)
        XCTAssertTrue(presentation.canPrepare)
        XCTAssertNil(presentation.playbackPath)
        XCTAssertEqual(presentation.preparationLabel, "録画ファイルを生成")
    }

    func testServerArtifactPathIsTheOnlyPlayablePath() throws {
        var detail = try detail()
        detail.playback = playback
        var presentation = RecordingPlaybackPresentation(detail: detail, playbackFileExists: true)
        XCTAssertEqual(presentation.playbackPath, playback.path)
        XCTAssertTrue(presentation.canPrepare, "An existing file may need hash repair or updated generation parameters")
        XCTAssertEqual(presentation.preparationLabel, "録画ファイルを再生成")

        detail.playback?.path = "playback.mp4"
        presentation = RecordingPlaybackPresentation(detail: detail, playbackFileExists: true)
        XCTAssertNil(presentation.playbackPath)
        XCTAssertTrue(presentation.canPrepare)
    }

    func testAvailablePlaybackCannotBeRegeneratedAfterSourceDiscard() throws {
        var detail = try detail()
        detail.playback = playback
        detail.recording.tracks["screen"]?.discarded = true
        let presentation = RecordingPlaybackPresentation(detail: detail, playbackFileExists: true)
        XCTAssertEqual(presentation.playbackPath, playback.path)
        XCTAssertFalse(presentation.canPrepare)
        XCTAssertTrue(presentation.message.contains("破棄"))
    }

    func testMissingArtifactOffersRepairFromRetainedTracks() throws {
        var detail = try detail()
        detail.playback = playback
        let presentation = RecordingPlaybackPresentation(detail: detail, playbackFileExists: false)
        XCTAssertNil(presentation.playbackPath)
        XCTAssertTrue(presentation.canPrepare)
        XCTAssertEqual(presentation.preparationLabel, "録画ファイルを再生成")
        XCTAssertTrue(presentation.message.contains("作り直せます"))
    }

    func testRecordingMustStopBeforePreparing() throws {
        var detail = try detail()
        detail.meeting.status = "recording"
        let presentation = RecordingPlaybackPresentation(detail: detail, playbackFileExists: false)
        XCTAssertFalse(presentation.canPrepare)
        XCTAssertNil(presentation.playbackPath)
        XCTAssertTrue(presentation.message.contains("停止後"))
    }

    func testMissingEmptyOrDiscardedScreenExplainsWhyPreparationIsUnavailable() throws {
        for state in ["missing", "empty", "discarded"] {
            var detail = try detail()
            if state == "missing" { detail.recording.tracks.removeValue(forKey: "screen") }
            if state == "empty" { detail.recording.tracks["screen"]?.bytes = 0 }
            if state == "discarded" { detail.recording.tracks["screen"]?.discarded = true }
            let presentation = RecordingPlaybackPresentation(detail: detail, playbackFileExists: false)
            XCTAssertFalse(presentation.canPrepare, state)
            XCTAssertTrue(presentation.message.contains("画面録画"), state)
            XCTAssertTrue(presentation.message.contains("生成できません"), state)
        }
    }

    func testOneRetainedAudioTrackIsEnoughButDiscardedOrEmptyAudioIsNot() throws {
        var detail = try detail()
        detail.recording.tracks["mic"]?.discarded = true
        XCTAssertTrue(RecordingPlaybackPresentation(detail: detail, playbackFileExists: false).canPrepare)
        detail.recording.tracks["system"]?.bytes = 0
        let presentation = RecordingPlaybackPresentation(detail: detail, playbackFileExists: false)
        XCTAssertFalse(presentation.canPrepare)
        XCTAssertTrue(presentation.message.contains("元の音声"))
    }

    func testInterruptedRecordingKeepsPlaybackAndShowsRetainedDataWarning() throws {
        var detail = try detail()
        detail.playback = playback
        detail.recording.recorderError = ToolErrorInfo(code: "recorder_error", message: "Stream stopped")
        let presentation = RecordingPlaybackPresentation(detail: detail, playbackFileExists: true)
        XCTAssertEqual(presentation.playbackPath, playback.path)
        XCTAssertTrue(try XCTUnwrap(presentation.recorderWarning).contains("保存できたデータは残っています"))
        detail.recording.tracks = [:]
        let missing = RecordingPlaybackPresentation(detail: detail, playbackFileExists: false)
        XCTAssertTrue(try XCTUnwrap(missing.recorderWarning).contains("保存できていない可能性"))
    }

    func testPlaybackAndRecorderErrorDecodeAlongsideDisplayMetadata() throws {
        var original = try detail()
        original.playback = playback
        original.recording.display = RecordingDisplay(id: 77, name: "内蔵画面", width: 1512, height: 982, isMain: true)
        original.recording.recorderError = ToolErrorInfo(code: "capture_stopped", message: "Display disconnected")
        let encoded = try JSONEncoder().encode(original)
        let result = try JSONDecoder().decode(MeetingDetail.self, from: encoded)
        XCTAssertEqual(result, original)
        let payload = try XCTUnwrap(JSONSerialization.jsonObject(with: encoded) as? [String: Any])
        let recording = try XCTUnwrap(payload["recording"] as? [String: Any])
        XCTAssertNotNil(recording["recorder_error"])
    }

    func testFastPlaybackCompletionRefreshesDetailOnlyOnce() throws {
        let complete = try JSONDecoder().decode(Job.self, from: Data(#"""
            {"job_id":"job-0123456789ab","meeting_id":"20260827T030500Z-a1b2c3d4",
             "kind":"playback","status":"succeeded","created_at":"2026-08-27T04:05:00Z",
             "updated_at":"2026-08-27T04:05:01Z"}
            """#.utf8))
        XCTAssertTrue(RecordingPlaybackPresentation.shouldRefreshDetail(after: complete, previous: nil))
        XCTAssertFalse(RecordingPlaybackPresentation.shouldRefreshDetail(after: complete, previous: complete))
        var running = complete
        running.status = "running"
        XCTAssertFalse(RecordingPlaybackPresentation.shouldRefreshDetail(after: running, previous: nil))
        XCTAssertTrue(RecordingPlaybackPresentation.shouldRefreshDetail(after: complete, previous: running))
        XCTAssertEqual(NarumiFormat.jobKindLabel("playback"), "録画ファイル生成")
    }

    func testLostPlaybackReceiptRetainsOriginalRequestAndArgumentsUntilRecovery() throws {
        var state = DesktopJobRequestState()
        let arguments = Data(#"{"meeting_id":"20260827T030500Z-a1b2c3d4","scope":"team","request_id":"prepare-1"}"#.utf8)
        let request = DesktopJobRequestState.Request(requestID: "prepare-1", tool: ToolCatalog.prepareRecording, arguments: arguments)
        let token = try XCTUnwrap(state.begin(request))
        XCTAssertTrue(state.markUncertain(token))
        XCTAssertEqual(state.pendingCount, 1)
        let retry = try XCTUnwrap(state.beginRetry())
        XCTAssertEqual(retry.request, request)
        XCTAssertTrue(state.confirm(retry.token))
        XCTAssertEqual(state.pendingCount, 0)
    }

    func testPlaybackAppearsBeforeTheMinutesJobFinishes() throws {
        var job = try JSONDecoder().decode(Job.self, from: Data(#"""
            {"job_id":"job-0123456789ab","kind":"process","status":"running",
             "progress":{"stage":"recording/playback","fraction":0},
             "created_at":"2026-08-27T04:05:00Z","updated_at":"2026-08-27T04:05:01Z"}
            """#.utf8))
        XCTAssertFalse(RecordingPlaybackPresentation.shouldRefreshPlayback(after: job, previous: nil))
        let generatingPlayback = job
        job.progress = JobProgress(stage: "transcribe", fraction: 0)
        XCTAssertTrue(RecordingPlaybackPresentation.shouldRefreshPlayback(after: job, previous: generatingPlayback))
        XCTAssertTrue(RecordingPlaybackPresentation.shouldRefreshPlayback(after: job, previous: nil))
        XCTAssertFalse(RecordingPlaybackPresentation.shouldRefreshPlayback(after: job, previous: job))
    }
}
