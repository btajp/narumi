import XCTest

@testable import NarumiMenuBarCore

final class RecordingDisplayTests: XCTestCase {
    private let secondary = RecordingDisplay(id: 12, name: "Display", width: 1920, height: 1080, isMain: false)
    private let main = RecordingDisplay(id: 42, name: "Display", width: 2560, height: 1440, isMain: true)

    func testMainDisplayIsDefaultRegardlessOfEnumerationOrder() throws {
        for displays in [[secondary, main], [main, secondary]] {
            let response = ListRecordingDisplaysResponse(displays: displays)
            let selection = try response.defaultSelectionIndex()
            XCTAssertEqual(displays[selection].id, main.id)
        }
    }

    func testDoesNotSilentlySelectSecondaryWhenMainIsMissing() {
        let response = ListRecordingDisplaysResponse(displays: [secondary])
        XCTAssertThrowsError(try response.defaultSelectionIndex()) { error in
            guard case RecordingDisplaySelectionError.mainDisplayUnavailable = error else {
                return XCTFail("Expected missing-main error, got \(error)")
            }
            XCTAssertTrue(error.localizedDescription.contains("録画を開始していません"))
        }
    }

    func testEmptyOrAmbiguousDisplayListsCannotStartRecording() {
        let anotherMain = RecordingDisplay(id: 99, name: "Display", width: 1920, height: 1080, isMain: true)
        for displays in [[], [main, main], [main, anotherMain]] {
            XCTAssertThrowsError(try ListRecordingDisplaysResponse(displays: displays).defaultSelectionIndex())
        }
    }

    func testSelectionTitlesIdentifySameNameDisplays() {
        XCTAssertNotEqual(main.selectionTitle, secondary.selectionTitle)
        XCTAssertTrue(main.selectionTitle.contains("2560 × 1440"))
        XCTAssertTrue(main.selectionTitle.contains("ID: 42"))
        XCTAssertTrue(main.selectionTitle.contains("メイン"))
        XCTAssertFalse(secondary.selectionTitle.contains("メイン"))
    }

    func testDecodesContractKeysAndFullUInt32DisplayID() throws {
        let data = Data(#"{"id":4294967295,"name":"Display","width":1920,"height":1080,"is_main":true}"#.utf8)
        let display = try JSONDecoder().decode(RecordingDisplay.self, from: data)
        XCTAssertEqual(display.id, UInt32.max)
        XCTAssertTrue(display.isMain)
        XCTAssertEqual(try JSONDecoder().decode(RecordingDisplay.self, from: JSONEncoder().encode(display)), display)
    }

    func testRejectsInvalidDisplayMetadata() throws {
        let valid: [String: Any] = ["id": 1, "name": "Display", "width": 1920, "height": 1080, "is_main": true]
        let invalidValues: [(String, Any)] = [
            ("id", 0), ("id", -1), ("id", UInt64(UInt32.max) + 1), ("id", 1.5), ("id", "1"),
            ("name", ""), ("name", " \n\t"), ("width", 0), ("height", -1), ("is_main", "true"),
        ]
        for (key, value) in invalidValues {
            var input = valid
            input[key] = value
            let data = try JSONSerialization.data(withJSONObject: input)
            XCTAssertThrowsError(try JSONDecoder().decode(RecordingDisplay.self, from: data), "\(key): \(value)")
        }
        for key in valid.keys {
            var input = valid
            input.removeValue(forKey: key)
            let data = try JSONSerialization.data(withJSONObject: input)
            XCTAssertThrowsError(try JSONDecoder().decode(RecordingDisplay.self, from: data), "missing \(key)")
        }
    }

    func testRecordingStatusRetainsReportedDisplay() throws {
        let data = Data(#"{"active":true,"display":{"id":42,"name":"Display","width":2560,"height":1440,"is_main":true}}"#.utf8)
        let status = try JSONDecoder().decode(RecordingStatus.self, from: data)
        XCTAssertEqual(status.display, main)
        let oldStatus = try JSONDecoder().decode(RecordingStatus.self, from: Data(#"{"active":false}"#.utf8))
        XCTAssertNil(oldStatus.display)
    }
}
