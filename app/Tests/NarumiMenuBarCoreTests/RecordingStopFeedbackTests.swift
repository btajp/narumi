import XCTest

@testable import NarumiMenuBarCore

final class RecordingStopFeedbackTests: XCTestCase {
    func testSuccessfulStopWithoutRecorderErrorHasNoWarning() throws {
        let data = Data(#"{"meeting_id":"meeting-1","job_id":"job-1","tracks":{}}"#.utf8)
        let feedback = try JSONDecoder().decode(RecordingStopFeedback.self, from: data)
        XCTAssertNil(feedback.warningMessage)
    }

    func testRecorderFailureWarnsEvenWhenStopAndTrackFinalizationSucceeded() throws {
        let data = Data(#"{"tracks":{"screen":{"path":"tracks/screen.mp4"}},"recorder_error":{"code":"recorder_unavailable","message":"Screen capture ended unexpectedly","details":{"reason":"stream_stopped"}}}"#.utf8)
        let feedback = try JSONDecoder().decode(RecordingStopFeedback.self, from: data)
        let warning = try XCTUnwrap(feedback.warningMessage)
        XCTAssertTrue(warning.contains("録画を停止"))
        XCTAssertTrue(warning.contains("保存できたトラックを保持"))
        XCTAssertTrue(warning.contains("正常に完了しなかった"))
        XCTAssertTrue(warning.contains("recorder_unavailable: Screen capture ended unexpectedly"))
    }

    func testMalformedRecorderFailureCannotBecomeUnqualifiedSuccess() {
        for json in [#"{"recorder_error":{"code":"recorder_unavailable"}}"#, #"{"recorder_error":null}"#] {
            XCTAssertThrowsError(try JSONDecoder().decode(RecordingStopFeedback.self, from: Data(json.utf8)))
        }
    }
}
