import Foundation
import XCTest

@testable import NarumiRecorderKit

@MainActor
final class PermissionSetupTests: XCTestCase {
    func testMicrophoneRequestReturnsFreshGrantedStateAndNoSettingsLaunch() async throws {
        let driver = FakePermissionSetupDriver(screen: .denied, microphone: .unknown)
        driver.microphoneAfterRequest = .granted

        let result = try await PermissionSetup.run(.requestPermission(.microphone), using: driver)

        XCTAssertEqual(result.permission, .microphone)
        XCTAssertEqual(result.action, .request)
        XCTAssertEqual(result.permissions, PermissionReport(screenRecording: .denied, microphone: .granted))
        XCTAssertFalse(result.settingsOpened)
        XCTAssertEqual(driver.requests, [.microphone])
        XCTAssertEqual(driver.checkCount, 2)
        XCTAssertTrue(driver.openedURLs.isEmpty)
    }

    func testMicrophoneDeniedRequestIsANormalResult() async throws {
        let driver = FakePermissionSetupDriver(screen: .granted, microphone: .unknown)
        driver.microphoneAfterRequest = .denied

        let result = try await PermissionSetup.run(.requestPermission(.microphone), using: driver)

        XCTAssertEqual(result.permissions.microphone, .denied)
        XCTAssertEqual(result.permissions.screenRecording, .granted)
        XCTAssertFalse(result.settingsOpened)
        XCTAssertEqual(driver.requests, [.microphone])
    }

    func testGrantedAndDeniedMicrophoneAreNotRequestedAgain() async throws {
        for status in [PermissionStatus.granted, .denied] {
            let driver = FakePermissionSetupDriver(screen: .denied, microphone: status)
            let result = try await PermissionSetup.run(.requestPermission(.microphone), using: driver)

            XCTAssertEqual(result.permissions.microphone, status)
            XCTAssertTrue(driver.requests.isEmpty)
            XCTAssertTrue(driver.openedURLs.isEmpty)
        }
    }

    func testAnUnchangedUnknownResultIsNotReportedAsGranted() async throws {
        let driver = FakePermissionSetupDriver(screen: .denied, microphone: .unknown)

        let result = try await PermissionSetup.run(.requestPermission(.microphone), using: driver)

        XCTAssertEqual(result.permissions.microphone, .unknown)
        XCTAssertEqual(driver.requests, [.microphone])
    }

    func testScreenRecordingRequestUsesOnlyScreenPermission() async throws {
        let driver = FakePermissionSetupDriver(screen: .denied, microphone: .unknown)
        driver.screenAfterRequest = .granted

        let result = try await PermissionSetup.run(.requestPermission(.screenRecording), using: driver)

        XCTAssertEqual(result.permissions.screenRecording, .granted)
        XCTAssertEqual(result.permissions.microphone, .unknown)
        XCTAssertEqual(driver.requests, [.screenRecording])
        XCTAssertFalse(result.settingsOpened)
        XCTAssertTrue(driver.openedURLs.isEmpty)
    }

    func testScreenRecordingDenialIsANormalResult() async throws {
        let driver = FakePermissionSetupDriver(screen: .denied, microphone: .granted)

        let result = try await PermissionSetup.run(.requestPermission(.screenRecording), using: driver)

        XCTAssertEqual(result.permissions.screenRecording, .denied)
        XCTAssertEqual(driver.requests, [.screenRecording])
        XCTAssertFalse(result.settingsOpened)
    }

    func testAlreadyGrantedScreenRecordingDoesNotPrompt() async throws {
        let driver = FakePermissionSetupDriver(screen: .granted, microphone: .unknown)

        let result = try await PermissionSetup.run(.requestPermission(.screenRecording), using: driver)

        XCTAssertEqual(result.permissions.screenRecording, .granted)
        XCTAssertTrue(driver.requests.isEmpty)
    }

    func testUnknownScreenRecordingDoesNotSkipTheExplicitRequest() async throws {
        let driver = FakePermissionSetupDriver(screen: .unknown, microphone: .unknown)

        let result = try await PermissionSetup.run(.requestPermission(.screenRecording), using: driver)

        XCTAssertEqual(result.permissions.screenRecording, .unknown)
        XCTAssertEqual(driver.requests, [.screenRecording])
    }

    func testSettingsCommandsUseOnlyTheirFixedURLsAndDoNotRequestPermission() async throws {
        let cases: [(RecordingPermission, String)] = [
            (.microphone, "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone"),
            (.screenRecording, "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture"),
        ]
        for (permission, expectedURL) in cases {
            let driver = FakePermissionSetupDriver(screen: .denied, microphone: .denied)
            let result = try await PermissionSetup.run(.openPermissionSettings(permission), using: driver)

            XCTAssertEqual(driver.openedURLs.map(\.absoluteString), [expectedURL])
            XCTAssertTrue(driver.requests.isEmpty)
            XCTAssertEqual(driver.checkCount, 1)
            XCTAssertEqual(result.permission, permission)
            XCTAssertEqual(result.action, .openSettings)
            XCTAssertTrue(result.settingsOpened)
            XCTAssertEqual(result.permissions, PermissionReport(screenRecording: .denied, microphone: .denied))
        }
    }

    func testSettingsFallbackRunsOnlyAfterTheTargetURLFails() async throws {
        let driver = FakePermissionSetupDriver(screen: .denied, microphone: .unknown)
        driver.openResults = [false, true]

        let result = try await PermissionSetup.run(.openPermissionSettings(.microphone), using: driver)

        XCTAssertEqual(driver.openedURLs.map(\.absoluteString), [
            "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone",
            "x-apple.systempreferences:com.apple.preference.security",
        ])
        XCTAssertTrue(result.settingsOpened)
        XCTAssertEqual(result.permissions.microphone, .unknown)
        XCTAssertTrue(driver.requests.isEmpty)
    }

    func testSettingsLaunchFailureUsesExistingRecorderErrorProtocol() async throws {
        let driver = FakePermissionSetupDriver(screen: .denied, microphone: .unknown)
        driver.openResults = [false, false]

        do {
            _ = try await PermissionSetup.run(.openPermissionSettings(.screenRecording), using: driver)
            XCTFail("both Settings launch attempts failed")
        } catch let error as RecorderError {
            XCTAssertEqual(error.code, .captureFailed)
            XCTAssertEqual(
                RecorderEventLine.encode(.error(ErrorEvent(error))),
                #"{"event":"error","code":"capture_failed","message":"cannot open macOS privacy settings"}"#)
        }
        XCTAssertEqual(driver.openedURLs.count, 2)
        XCTAssertTrue(driver.requests.isEmpty)
    }

    func testParsedPermissionCommandRunsTheSameFakeOnlyPathAndSerializesContract() async throws {
        let driver = FakePermissionSetupDriver(screen: .denied, microphone: .unknown)
        driver.microphoneAfterRequest = .granted
        let command = try ArgumentParser.parse(["request-permission", "microphone"])

        let result = try await PermissionSetup.run(command, using: driver)

        XCTAssertEqual(
            result.serialized(),
            #"{"permission":"microphone","action":"request","permissions":{"screen_recording":"denied","microphone":"granted"},"settings_opened":false}"#)
        let json = try XCTUnwrap(
            JSONSerialization.jsonObject(with: Data(result.serialized().utf8)) as? [String: Any])
        XCTAssertEqual(Set(json.keys), ["permission", "action", "permissions", "settings_opened"])
        XCTAssertEqual(json["settings_opened"] as? Bool, false)
        XCTAssertEqual(driver.requests, [.microphone])
        XCTAssertTrue(driver.openedURLs.isEmpty)
    }

    func testParsedSettingsCommandSerializesTrueWithoutClaimingPermissionGranted() async throws {
        let driver = FakePermissionSetupDriver(screen: .denied, microphone: .denied)
        let command = try ArgumentParser.parse(["open-permission-settings", "screen_recording"])

        let result = try await PermissionSetup.run(command, using: driver)

        XCTAssertEqual(
            result.serialized(),
            #"{"permission":"screen_recording","action":"open_settings","permissions":{"screen_recording":"denied","microphone":"denied"},"settings_opened":true}"#)
        XCTAssertTrue(driver.requests.isEmpty)
    }

    func testNonPermissionCommandsAreRejectedWithoutAnyOSOrRecordingOperation() async throws {
        let driver = FakePermissionSetupDriver(screen: .denied, microphone: .unknown)
        let commands: [RecorderCommand] = [
            .record(RecordOptions(outputDir: "/unused")), .check, .listDisplays, .help,
        ]
        for command in commands {
            do {
                _ = try await PermissionSetup.run(command, using: driver)
                XCTFail("a permission handler must not execute other commands")
            } catch let error as RecorderError {
                XCTAssertEqual(error.code, .invalidArgument)
            }
        }
        XCTAssertEqual(driver.checkCount, 0)
        XCTAssertTrue(driver.requests.isEmpty)
        XCTAssertTrue(driver.openedURLs.isEmpty)
    }
}

@MainActor
private final class FakePermissionSetupDriver: PermissionSetupDriver {
    var report: PermissionReport
    var microphoneAfterRequest: PermissionStatus?
    var screenAfterRequest: PermissionStatus?
    var openResults: [Bool] = []
    private(set) var requests: [RecordingPermission] = []
    private(set) var openedURLs: [URL] = []
    private(set) var checkCount = 0

    init(screen: PermissionStatus, microphone: PermissionStatus) {
        report = PermissionReport(screenRecording: screen, microphone: microphone)
    }

    func check() -> PermissionReport {
        checkCount += 1
        return report
    }

    func requestMicrophoneAccess() async {
        requests.append(.microphone)
        if let microphoneAfterRequest {
            report.microphone = microphoneAfterRequest
        }
    }

    func requestScreenRecordingAccess() {
        requests.append(.screenRecording)
        if let screenAfterRequest {
            report.screenRecording = screenAfterRequest
        }
    }

    func openSettingsURL(_ url: URL) -> Bool {
        openedURLs.append(url)
        return openResults.isEmpty ? true : openResults.removeFirst()
    }
}
