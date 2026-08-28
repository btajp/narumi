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

    func testPermissionCommandsAcceptOnlyTheFixedPermissionNames() throws {
        for permission in RecordingPermission.allCases {
            XCTAssertEqual(
                try ArgumentParser.parse(["request-permission", permission.rawValue]),
                .requestPermission(permission))
            XCTAssertEqual(
                try ArgumentParser.parse(["open-permission-settings", permission.rawValue]),
                .openPermissionSettings(permission))
        }
    }

    func testPermissionCommandsRejectMissingUnknownAndExtraArguments() {
        for command in ["request-permission", "open-permission-settings"] {
            let invalidArguments = [
                [], ["camera"], ["Microphone"], ["screen-recording"], [""],
                ["https://example.invalid"], ["microphone", "screen_recording"],
                ["microphone", "--output", "/tmp/tracks"], ["--permission=microphone"],
            ]
            for arguments in invalidArguments {
                XCTAssertThrowsError(try ArgumentParser.parse([command] + arguments)) { error in
                    XCTAssertEqual((error as? RecorderError)?.code, .invalidArgument)
                }
            }
        }
    }

    func testHelpDescribesPermissionCommandsAsNonRecordingActions() {
        XCTAssertTrue(ArgumentParser.usage.contains("request-permission microphone|screen_recording"))
        XCTAssertTrue(ArgumentParser.usage.contains("open-permission-settings microphone|screen_recording"))
        XCTAssertTrue(ArgumentParser.usage.contains("without starting a recording"))
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
