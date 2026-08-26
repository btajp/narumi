import Foundation
import XCTest

@testable import NarumiRecorderKit

final class FileNamingTests: XCTestCase {
    func testFixedFileNames() {
        XCTAssertEqual(TrackFileNames.fileName(for: .screen), "screen.mp4")
        XCTAssertEqual(TrackFileNames.fileName(for: .mic), "mic.m4a")
        XCTAssertEqual(TrackFileNames.fileName(for: .system), "system.m4a")
        XCTAssertEqual(TrackFileNames.recorderJSON, "recorder.json")
    }

    func testTrackOrderWithAndWithoutVideo() {
        XCTAssertEqual(TrackFileNames.tracks(includeVideo: true), [.screen, .mic, .system])
        XCTAssertEqual(TrackFileNames.tracks(includeVideo: false), [.mic, .system])
        XCTAssertEqual(TrackKind.allCases, [.screen, .mic, .system])
    }

    func testURLsAreInsideOutputDir() {
        let dir = URL(fileURLWithPath: "/tmp/meeting/tracks", isDirectory: true)
        XCTAssertEqual(TrackFileNames.url(for: .screen, in: dir).path, "/tmp/meeting/tracks/screen.mp4")
        XCTAssertEqual(TrackFileNames.url(for: .mic, in: dir).path, "/tmp/meeting/tracks/mic.m4a")
        XCTAssertEqual(TrackFileNames.url(for: .system, in: dir).path, "/tmp/meeting/tracks/system.m4a")
        XCTAssertEqual(TrackFileNames.recorderJSONURL(in: dir).path, "/tmp/meeting/tracks/recorder.json")
    }

    func testStartedTracksMatchFileNames() {
        XCTAssertEqual(
            StartedTracks.standard(includeVideo: true),
            StartedTracks(screen: "screen.mp4", mic: "mic.m4a", system: "system.m4a"))
        XCTAssertEqual(StartedTracks.standard(includeVideo: false), StartedTracks(screen: nil, mic: "mic.m4a", system: "system.m4a"))
    }

    func testPermissionReportJSON() {
        let report = PermissionReport(screenRecording: .granted, microphone: .unknown)
        XCTAssertEqual(report.serialized(), #"{"screen_recording":"granted","microphone":"unknown"}"#)
    }

    func testRecorderJSONIsWrittenAtomically() throws {
        let dir = FileManager.default.temporaryDirectory
            .appendingPathComponent("narumi-recorder-tests-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: dir) }

        let summary = RecorderSummary(
            startedAt: "2026-08-27T03:05:00Z", stoppedAt: "2026-08-27T03:05:10Z", durationSec: 10,
            tracks: StoppedTracks(
                screen: nil,
                mic: TrackSummary(path: "mic.m4a", bytes: 1, durationSec: 10),
                system: TrackSummary(path: "system.m4a", bytes: 2, durationSec: 10)),
            recorderVersion: "0.1.0")
        try RecorderSession.writeRecorderJSON(summary, in: dir)
        try RecorderSession.writeRecorderJSON(summary, in: dir)  // overwrite path

        let url = TrackFileNames.recorderJSONURL(in: dir)
        let text = try String(contentsOf: url, encoding: .utf8)
        XCTAssertEqual(text, summary.serialized() + "\n")
        XCTAssertFalse(FileManager.default.fileExists(atPath: url.appendingPathExtension("tmp").path))
    }
}
