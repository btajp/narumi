import XCTest

@testable import NarumiRecorderKit

final class DisplaySelectionTests: XCTestCase {
    private let displays = [
        DisplayInfo(id: 1, width: 1728, height: 1117, name: "Built-in Retina Display"),
        DisplayInfo(id: 2, width: 3840, height: 2160, name: "LG UltraFine"),
    ]

    func testDefaultsToFirstDisplay() throws {
        XCTAssertEqual(try DisplaySelection.select(from: displays, requestedID: nil), displays[0])
    }

    func testSelectsRequestedDisplay() throws {
        XCTAssertEqual(try DisplaySelection.select(from: displays, requestedID: 2), displays[1])
    }

    func testUnknownDisplayIsNoDisplayError() {
        XCTAssertThrowsError(try DisplaySelection.select(from: displays, requestedID: 9)) { error in
            XCTAssertEqual((error as? RecorderError)?.code, .noDisplay)
            XCTAssertTrue((error as? RecorderError)?.message.contains("available: 1, 2") == true)
        }
    }

    func testEmptyListIsNoDisplayError() {
        XCTAssertThrowsError(try DisplaySelection.select(from: [], requestedID: nil)) { error in
            XCTAssertEqual((error as? RecorderError)?.code, .noDisplay)
        }
    }

    func testListDisplaysJSON() {
        XCTAssertEqual(
            DisplayInfo.jsonArray(displays),
            #"[{"id":1,"width":1728,"height":1117,"name":"Built-in Retina Display"},{"id":2,"width":3840,"height":2160,"name":"LG UltraFine"}]"#
        )
        XCTAssertEqual(DisplayInfo.jsonArray([]), "[]")
    }

    func testVideoDimensionsCapWidthAndKeepAspect() {
        XCTAssertEqual(VideoDimensions.fit(width: 3456, height: 2234), VideoDimensions(width: 1920, height: 1242))
        XCTAssertEqual(VideoDimensions.fit(width: 1920, height: 1080), VideoDimensions(width: 1920, height: 1080))
        XCTAssertEqual(VideoDimensions.fit(width: 1440, height: 900), VideoDimensions(width: 1440, height: 900))
        XCTAssertEqual(VideoDimensions.fit(width: 1727, height: 1117), VideoDimensions(width: 1728, height: 1118))
        XCTAssertEqual(VideoDimensions.fit(width: 3840, height: 2160, maxWidth: 1280), VideoDimensions(width: 1280, height: 720))
    }
}
