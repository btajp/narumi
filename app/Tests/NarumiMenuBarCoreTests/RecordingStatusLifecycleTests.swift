import Foundation
import XCTest

@testable import NarumiMenuBarCore

final class RecordingStatusLifecycleTests: XCTestCase {
    func testLegacyStatusWithoutRecorderAliveDecodesAsUnknown() throws {
        let data = Data(#"{"active":true,"meeting_id":"meeting-1","elapsed_sec":90}"#.utf8)

        let status = try JSONDecoder().decode(RecordingStatus.self, from: data)

        XCTAssertTrue(status.active)
        XCTAssertEqual(status.meetingID, "meeting-1")
        XCTAssertEqual(status.elapsedSec, 90)
        XCTAssertNil(status.recorderAlive)
    }

    func testActiveStatusCanReportRecorderExited() throws {
        let data = Data(#"{"active":true,"meeting_id":"meeting-1","recorder_alive":false}"#.utf8)

        let status = try JSONDecoder().decode(RecordingStatus.self, from: data)

        XCTAssertTrue(status.active)
        XCTAssertEqual(status.recorderAlive, false)
    }

    func testActiveStatusCanReportRecorderAlive() throws {
        let data = Data(#"{"active":true,"meeting_id":"meeting-1","recorder_alive":true}"#.utf8)

        let status = try JSONDecoder().decode(RecordingStatus.self, from: data)

        XCTAssertTrue(status.active)
        XCTAssertEqual(status.recorderAlive, true)
    }

    func testRecorderAliveEncodesUsingContractKeyAndRoundTripsBothValues() throws {
        for recorderAlive in [false, true] {
            let status = RecordingStatus(active: true, meetingID: "meeting-1", recorderAlive: recorderAlive)

            let data = try JSONEncoder().encode(status)
            let object = try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])

            XCTAssertEqual(object["recorder_alive"] as? Bool, recorderAlive)
            XCTAssertNil(object["recorderAlive"])
            XCTAssertEqual(try JSONDecoder().decode(RecordingStatus.self, from: data), status)
        }
    }

    func testDefaultRecorderAliveRemainsUnknownAndIsOmittedWhenEncoding() throws {
        let status = RecordingStatus(active: false)

        let data = try JSONEncoder().encode(status)
        let object = try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])

        XCTAssertNil(status.recorderAlive)
        XCTAssertNil(object["recorder_alive"])
        XCTAssertNil(object["recorderAlive"])
        XCTAssertEqual(try JSONDecoder().decode(RecordingStatus.self, from: data), status)
    }
}
