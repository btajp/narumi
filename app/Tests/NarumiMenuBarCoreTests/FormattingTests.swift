import XCTest

@testable import NarumiMenuBarCore

final class FormattingTests: XCTestCase {
    // MARK: Duration / timecode

    func testDuration() {
        XCTAssertEqual(NarumiFormat.duration(0), "0:00")
        XCTAssertEqual(NarumiFormat.duration(4.9), "0:04")  // floored
        XCTAssertEqual(NarumiFormat.duration(754.3), "12:34")
        XCTAssertEqual(NarumiFormat.duration(3754), "1:02:34")
        XCTAssertEqual(NarumiFormat.duration(-5), "0:00")  // negative clamps
    }

    func testTimecodeMatchesDuration() {
        XCTAssertEqual(NarumiFormat.timecode(130.2), "2:10")
    }

    // MARK: Timestamps

    func testParseTimestampPlainAndFractional() {
        XCTAssertNotNil(NarumiFormat.parseTimestamp("2026-08-27T03:05:00Z"))
        XCTAssertNotNil(NarumiFormat.parseTimestamp("2026-08-27T03:05:00.123Z"))
        XCTAssertNil(NarumiFormat.parseTimestamp("not a timestamp"))
    }

    func testJSTDateTime() {
        // 03:05 UTC = 12:05 JST (+9, no DST).
        XCTAssertEqual(NarumiFormat.jstDateTime("2026-08-27T03:05:00Z"), "2026-08-27 12:05")
        // Date rollover across midnight JST.
        XCTAssertEqual(NarumiFormat.jstDateTime("2026-08-27T16:30:00Z"), "2026-08-28 01:30")
        // Unparseable input passes through verbatim.
        XCTAssertEqual(NarumiFormat.jstDateTime("garbage"), "garbage")
    }

    // MARK: Labels

    func testStatusLabels() {
        XCTAssertEqual(NarumiFormat.meetingStatusLabel("recording"), "録画中")
        XCTAssertEqual(NarumiFormat.meetingStatusLabel("ready"), "完了")
        XCTAssertEqual(NarumiFormat.meetingStatusLabel("unknown-status"), "unknown-status")
        XCTAssertEqual(NarumiFormat.jobKindLabel("process"), "処理")
        XCTAssertEqual(NarumiFormat.jobStatusLabel("running"), "実行中")
        XCTAssertEqual(NarumiFormat.jobStatusLabel("cancelled"), "キャンセル")
    }

    func testJobText() {
        XCTAssertEqual(
            NarumiFormat.jobText(
                kind: "process", status: "running",
                progress: JobProgress(stage: "transcribe", fraction: 0.4)),
            "処理 実行中 (transcribe 40%)")
        XCTAssertEqual(
            NarumiFormat.jobText(kind: "export", status: "queued", progress: nil),
            "エクスポート 待機中")
        XCTAssertEqual(
            NarumiFormat.jobText(
                kind: "regenerate", status: "running", progress: JobProgress(fraction: 1.7)),
            "再生成 実行中 (100%)")  // fraction clamps to 0...1
    }

    // MARK: Scope input

    func testParseScopeInput() {
        XCTAssertEqual(NarumiFormat.parseScopeInput(""), [])
        XCTAssertEqual(NarumiFormat.parseScopeInput("cloudnative"), ["cloudnative"])
        XCTAssertEqual(
            NarumiFormat.parseScopeInput("cloudnative, btcon  cloudnative"),
            ["cloudnative", "btcon"])  // separators mixed, duplicates dropped, order kept
    }

    // MARK: Row presentation

    func testMeetingRowPresentation() {
        let meeting = MeetingSummary(
            meetingID: "20260827T030500Z-a1b2c3d4", meetingName: "週次定例",
            scope: "cloudnative", status: "processing", startedAt: "2026-08-27T03:05:00Z",
            activeJob: ActiveJob(
                jobID: "job-1", kind: "process", status: "running",
                progress: JobProgress(stage: "transcribe", fraction: 0.4)))
        let row = MeetingRowPresentation(meeting: meeting)
        XCTAssertEqual(row.title, "週次定例")
        XCTAssertEqual(row.statusLabel, "処理中")
        XCTAssertEqual(row.subtitle, "2026-08-27 12:05 · 処理中 · cloudnative")
        XCTAssertEqual(row.jobText, "処理 実行中 (transcribe 40%)")
    }

    func testMeetingRowPresentationWithoutJobOrScope() {
        let meeting = MeetingSummary(
            meetingID: "m1", meetingName: "会議", status: "ready",
            startedAt: "2026-08-27T03:05:00Z",
            activeJob: ActiveJob(jobID: "job-2", kind: "export", status: "succeeded"))
        let row = MeetingRowPresentation(meeting: meeting)
        XCTAssertEqual(row.subtitle, "2026-08-27 12:05 · 完了")
        XCTAssertNil(row.jobText, "finished jobs show no badge")
    }
}
