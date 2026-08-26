import XCTest

@testable import NarumiRecorderKit

final class ArgumentParserTests: XCTestCase {
    func testRecordWithAllOptions() throws {
        let command = try ArgumentParser.parse([
            "record", "--output", "/tmp/tracks", "--display", "5", "--no-video", "--mic", "BuiltInMic-UID",
        ])
        XCTAssertEqual(
            command,
            .record(RecordOptions(
                outputDir: "/tmp/tracks", displayID: 5, includeVideo: false, microphoneDeviceUID: "BuiltInMic-UID")))
    }

    func testRecordDefaults() throws {
        let command = try ArgumentParser.parse(["record", "--output", "/tmp/tracks"])
        XCTAssertEqual(command, .record(RecordOptions(outputDir: "/tmp/tracks")))
    }

    func testRecordAcceptsEqualsForm() throws {
        let command = try ArgumentParser.parse(["record", "--output=/tmp/x", "--display=2", "--mic=abc"])
        XCTAssertEqual(
            command, .record(RecordOptions(outputDir: "/tmp/x", displayID: 2, includeVideo: true, microphoneDeviceUID: "abc")))
    }

    func testRecordRequiresOutput() {
        XCTAssertThrowsError(try ArgumentParser.parse(["record"])) { error in
            XCTAssertEqual((error as? RecorderError)?.code, .invalidArgument)
        }
        XCTAssertThrowsError(try ArgumentParser.parse(["record", "--output"])) { error in
            XCTAssertEqual((error as? RecorderError)?.code, .invalidArgument)
        }
    }

    func testRecordRejectsBadDisplayAndUnknownFlags() {
        XCTAssertThrowsError(try ArgumentParser.parse(["record", "--output", "/tmp/x", "--display", "abc"])) { error in
            XCTAssertEqual((error as? RecorderError)?.code, .invalidArgument)
        }
        XCTAssertThrowsError(try ArgumentParser.parse(["record", "--output", "/tmp/x", "--fps", "30"])) { error in
            XCTAssertEqual((error as? RecorderError)?.code, .invalidArgument)
        }
        XCTAssertThrowsError(try ArgumentParser.parse(["record", "--output", "/tmp/x", "--no-video=1"])) { error in
            XCTAssertEqual((error as? RecorderError)?.code, .invalidArgument)
        }
    }

    func testOtherCommands() throws {
        XCTAssertEqual(try ArgumentParser.parse(["check"]), .check)
        XCTAssertEqual(try ArgumentParser.parse(["list-displays"]), .listDisplays)
        XCTAssertEqual(try ArgumentParser.parse(["help"]), .help)
        XCTAssertEqual(try ArgumentParser.parse(["--help"]), .help)
    }

    func testUnknownOrMissingCommand() {
        XCTAssertThrowsError(try ArgumentParser.parse([])) { error in
            XCTAssertEqual((error as? RecorderError)?.code, .invalidArgument)
        }
        XCTAssertThrowsError(try ArgumentParser.parse(["capture"])) { error in
            XCTAssertEqual((error as? RecorderError)?.code, .invalidArgument)
        }
        XCTAssertThrowsError(try ArgumentParser.parse(["check", "--verbose"])) { error in
            XCTAssertEqual((error as? RecorderError)?.code, .invalidArgument)
        }
    }
}
