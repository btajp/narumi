import Foundation
import XCTest

@testable import NarumiRecorderKit

final class RecorderEventsTests: XCTestCase {
    func testStartedLineMatchesProtocol() {
        let event = RecorderEvent.started(
            StartedEvent(startedAt: "2026-08-27T03:05:00Z", tracks: .standard(includeVideo: true)))
        XCTAssertEqual(
            RecorderEventLine.encode(event),
            #"{"event":"started","started_at":"2026-08-27T03:05:00Z","tracks":{"screen":"screen.mp4","mic":"mic.m4a","system":"system.m4a"}}"#
        )
    }

    func testStartedLineWithoutVideoOmitsScreen() {
        let event = RecorderEvent.started(
            StartedEvent(startedAt: "2026-08-27T03:05:00Z", tracks: .standard(includeVideo: false)))
        XCTAssertEqual(
            RecorderEventLine.encode(event),
            #"{"event":"started","started_at":"2026-08-27T03:05:00Z","tracks":{"mic":"mic.m4a","system":"system.m4a"}}"#
        )
    }

    func testStoppedLineMatchesProtocol() {
        let event = RecorderEvent.stopped(
            StoppedEvent(
                stoppedAt: "2026-08-27T03:07:03Z",
                durationSec: 123.4,
                tracks: StoppedTracks(
                    screen: TrackSummary(path: "screen.mp4", bytes: 1234, durationSec: 123.4),
                    mic: TrackSummary(path: "mic.m4a", bytes: 567, durationSec: 123.38),
                    system: TrackSummary(path: "system.m4a", bytes: 890, durationSec: 123.4)
                )))
        XCTAssertEqual(
            RecorderEventLine.encode(event),
            #"{"event":"stopped","stopped_at":"2026-08-27T03:07:03Z","duration_sec":123.4,"tracks":{"screen":{"path":"screen.mp4","bytes":1234,"duration_sec":123.4},"mic":{"path":"mic.m4a","bytes":567,"duration_sec":123.38},"system":{"path":"system.m4a","bytes":890,"duration_sec":123.4}}}"#
        )
    }

    func testErrorAndLogLines() {
        XCTAssertEqual(
            RecorderEventLine.encode(.error(ErrorEvent(code: .permissionDenied, message: "screen recording not permitted"))),
            #"{"event":"error","code":"permission_denied","message":"screen recording not permitted"}"#
        )
        XCTAssertEqual(
            RecorderEventLine.encode(.log(LogEvent(message: "hello \"world\"\n"))),
            #"{"event":"log","message":"hello \"world\"\n"}"#
        )
    }

    func testErrorCodesAreTheProtocolSet() {
        XCTAssertEqual(
            RecorderErrorCode.allCases.map(\.rawValue),
            ["permission_denied", "no_display", "capture_failed", "writer_failed", "invalid_argument"]
        )
    }

    func testDurationIsRoundedToMilliseconds() {
        let event = RecorderEvent.stopped(
            StoppedEvent(
                stoppedAt: "2026-08-27T03:07:03Z",
                durationSec: 0.1 + 0.2,
                tracks: StoppedTracks(
                    screen: nil,
                    mic: TrackSummary(path: "mic.m4a", bytes: 1, durationSec: 1.0 / 3.0),
                    system: TrackSummary(path: "system.m4a", bytes: 1, durationSec: 2)
                )))
        XCTAssertEqual(
            RecorderEventLine.encode(event),
            #"{"event":"stopped","stopped_at":"2026-08-27T03:07:03Z","duration_sec":0.3,"tracks":{"mic":{"path":"mic.m4a","bytes":1,"duration_sec":0.333},"system":{"path":"system.m4a","bytes":1,"duration_sec":2.0}}}"#
        )
    }

    func testLinesAreParseableByFoundationAndRoundTripThroughCodable() throws {
        let events: [RecorderEvent] = [
            .started(StartedEvent(startedAt: "2026-08-27T03:05:00Z", tracks: .standard(includeVideo: true))),
            .stopped(StoppedEvent(
                stoppedAt: "2026-08-27T03:07:03Z", durationSec: 12.5,
                tracks: StoppedTracks(
                    screen: TrackSummary(path: "screen.mp4", bytes: 10, durationSec: 12.5),
                    mic: TrackSummary(path: "mic.m4a", bytes: 20, durationSec: 12.4),
                    system: TrackSummary(path: "system.m4a", bytes: 30, durationSec: 12.5)))),
            .error(ErrorEvent(code: .writerFailed, message: "boom 日本語 \\ /")),
            .log(LogEvent(message: "tab\tnewline\ncontrol\u{01}")),
        ]
        let decoder = JSONDecoder()
        for event in events {
            let line = RecorderEventLine.encode(event)
            XCTAssertFalse(line.contains("\n"), "one object per line: \(line)")
            let data = Data(line.utf8)
            let object = try JSONSerialization.jsonObject(with: data) as? [String: Any]
            XCTAssertEqual(object?["event"] as? String, event.name)
            let decoded = try decoder.decode(RecorderEvent.self, from: data)
            XCTAssertEqual(decoded, event)
        }
    }

    func testRecorderSummaryJSON() throws {
        let summary = RecorderSummary(
            startedAt: "2026-08-27T03:05:00Z",
            stoppedAt: "2026-08-27T03:07:03Z",
            durationSec: 123.4,
            tracks: StoppedTracks(
                screen: TrackSummary(path: "screen.mp4", bytes: 1234, durationSec: 123.4),
                mic: TrackSummary(path: "mic.m4a", bytes: 567, durationSec: 123.4),
                system: TrackSummary(path: "system.m4a", bytes: 890, durationSec: 123.4)),
            recorderVersion: "0.1.0"
        )
        XCTAssertEqual(
            summary.serialized(),
            #"{"started_at":"2026-08-27T03:05:00Z","stopped_at":"2026-08-27T03:07:03Z","duration_sec":123.4,"tracks":{"screen":{"path":"screen.mp4","bytes":1234,"duration_sec":123.4},"mic":{"path":"mic.m4a","bytes":567,"duration_sec":123.4},"system":{"path":"system.m4a","bytes":890,"duration_sec":123.4}},"recorder_version":"0.1.0"}"#
        )
        let stopped = try XCTUnwrap(summary.stoppedEvent)
        XCTAssertEqual(stopped.durationSec, 123.4)
        let decoded = try JSONDecoder().decode(RecorderSummary.self, from: Data(summary.serialized().utf8))
        XCTAssertEqual(decoded, summary)

        let failed = RecorderSummary(
            startedAt: "2026-08-27T03:05:00Z", stoppedAt: "2026-08-27T03:05:01Z", durationSec: 1,
            tracks: nil, recorderVersion: "0.1.0", error: ErrorEvent(code: .captureFailed, message: "stream stopped"))
        XCTAssertNil(failed.stoppedEvent)
        XCTAssertEqual(
            failed.serialized(),
            #"{"started_at":"2026-08-27T03:05:00Z","stopped_at":"2026-08-27T03:05:01Z","duration_sec":1.0,"tracks":{},"recorder_version":"0.1.0","error":{"code":"capture_failed","message":"stream stopped"}}"#
        )
    }

    func testCollectingSinkKeepsOrder() {
        let sink = CollectingEventSink()
        sink.emit(.log(LogEvent(message: "a")))
        sink.emit(.error(ErrorEvent(code: .noDisplay, message: "b")))
        XCTAssertEqual(sink.lines, [
            #"{"event":"log","message":"a"}"#,
            #"{"event":"error","code":"no_display","message":"b"}"#,
        ])
    }

    func testTimestampIsUTCSecondPrecision() {
        let date = Date(timeIntervalSince1970: 1_787_799_900)  // 2026-08-27T03:05:00Z
        XCTAssertEqual(Timestamps.iso8601(date), "2026-08-27T03:05:00Z")
        XCTAssertEqual(Timestamps.iso8601(date.addingTimeInterval(0.75)), "2026-08-27T03:05:00Z")
    }
}
