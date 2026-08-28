import Foundation
import XCTest

@testable import NarumiMenuBarCore

final class RecordingPermissionModelsTests: XCTestCase {
    private let instanceID = "12345678-9abc-4def-8abc-123456789abc"

    func testEnumsUseExactContractValues() throws {
        XCTAssertEqual(RecordingPermission.allCases.map(\.rawValue), ["microphone", "screen_recording"])
        XCTAssertEqual(RecordingPermission.allCases.map(\.id), ["microphone", "screen_recording"])
        XCTAssertEqual(RecordingPermissionAction.allCases.map(\.rawValue), ["request", "open_settings"])
        for permission in RecordingPermission.allCases {
            let data = try JSONEncoder().encode(permission)
            XCTAssertEqual(try JSONDecoder().decode(RecordingPermission.self, from: data), permission)
        }
    }

    func testPermissionResponseAcceptsAllKnownStatesAndRoundTrips() throws {
        for screen in ["granted", "denied", "unknown"] {
            for microphone in ["granted", "denied", "unknown"] {
                for action in RecordingPermissionAction.allCases {
                    let response = ConfigureRecordingPermissionResponse(
                        permission: .screenRecording, action: action,
                        permissions: .init(screenRecording: screen, microphone: microphone),
                        settingsOpened: action == .openSettings)
                    let data = try JSONEncoder().encode(response)
                    XCTAssertEqual(
                        try JSONDecoder().decode(ConfigureRecordingPermissionResponse.self, from: data), response)
                }
            }
        }
    }

    func testPermissionResponseRejectsMissingOrNullRequiredFields() throws {
        for key in ["permission", "action", "permissions", "settings_opened"] {
            var object = responseObject()
            object.removeValue(forKey: key)
            XCTAssertThrowsError(try decodeResponse(object), "missing \(key)")
            object[key] = NSNull()
            XCTAssertThrowsError(try decodeResponse(object), "null \(key)")
        }
        for key in ["screen_recording", "microphone"] {
            var object = responseObject()
            var permissions = try XCTUnwrap(object["permissions"] as? [String: Any])
            permissions.removeValue(forKey: key)
            object["permissions"] = permissions
            XCTAssertThrowsError(try decodeResponse(object), "missing permissions.\(key)")
        }
    }

    func testPermissionResponseRejectsUnknownStatesAndWrongTypes() throws {
        let invalidStates: [Any] = ["authorized", "not_determined", "GRANTED", "", 1, true, NSNull()]
        for key in ["screen_recording", "microphone"] {
            for value in invalidStates {
                var object = responseObject()
                var permissions = try XCTUnwrap(object["permissions"] as? [String: Any])
                permissions[key] = value
                object["permissions"] = permissions
                XCTAssertThrowsError(try decodeResponse(object), "invalid permissions.\(key)")
            }
        }
        for (key, value) in [("permission", "camera"), ("action", "reset"), ("settings_opened", "true")] {
            var object = responseObject()
            object[key] = value
            XCTAssertThrowsError(try decodeResponse(object), "invalid \(key)")
        }
    }

    func testLegacyServerCapabilitiesOmitPermissionSetupFlag() throws {
        let capabilities = try decodeCapabilities(capabilitiesObject())
        XCTAssertFalse(capabilities.permissionSetupInProgress)
        XCTAssertNil(capabilities.permissions)
        let initialized = ServerCapabilities(
            recording: false, transports: ["streamable-http"], transcriptionEngines: ["fake"],
            diarizationEngines: ["none"], llmProviders: ["none"], exportDestinations: ["markdown"])
        XCTAssertEqual(capabilities, initialized)
    }

    func testServerCapabilitiesPreservePermissionSetupFlag() throws {
        for value in [true, false] {
            var object = capabilitiesObject()
            object["permission_setup_in_progress"] = value
            let capabilities = try decodeCapabilities(object)
            XCTAssertEqual(capabilities.permissionSetupInProgress, value)
            let data = try JSONEncoder().encode(capabilities)
            XCTAssertEqual(try JSONDecoder().decode(ServerCapabilities.self, from: data), capabilities)
        }
    }

    func testServerCapabilitiesDoNotTreatMalformedFlagAsIdle() {
        let invalidValues: [Any] = [NSNull(), "false", 0, [String](), [String: String]()]
        for value in invalidValues {
            var object = capabilitiesObject()
            object["permission_setup_in_progress"] = value
            XCTAssertThrowsError(try decodeCapabilities(object))
        }
    }

    func testSetupSupportRequiresKnownMajorAndMinimumSemanticVersion() {
        let cases: [(String?, Bool)] = [
            (nil, false), ("1.0.0", false), ("1.0.99", false), ("0.99.99", false),
            ("1.1.0", true), ("1.1.1", true), ("1.2.0", true), ("1.10.0", true),
            ("1.1.0-rc.1", false), ("1.1.1-rc.1", true), ("1.2.0-alpha", true),
            ("2.0.0", false), ("2.1.0", false), ("99.99.99", false),
        ]
        for (version, supported) in cases {
            XCTAssertEqual(RecordingPermissionContract.supportsSetup(version), supported, version ?? "nil")
        }
    }

    func testMalformedContractVersionsNeverEnableSetup() {
        let versions = [
            "", "1", "1.1", "1.1.0.0", "v1.1.0", " 1.1.0", "1.1.0\n", "1.1.x",
            "01.1.0", "1.01.0", "1.1.00", "1.-1.0", "1.1.0-", "1.2.0-alpha..1",
            "1.2.0-01", "1.2.0-.alpha", "1.2.0-alpha.", "1.2.0-alpha_1", "1.1.0+build.1",
            "1.999999999999999999999999.0",
        ]
        for version in versions {
            XCTAssertFalse(RecordingPermissionContract.supportsSetup(version), version)
        }
    }

    func testServerInstanceIDRequiresCanonicalLowercaseUUIDv4() {
        for variant in ["8", "9", "a", "b"] {
            XCTAssertTrue(RecordingPermissionContract.isValidServerInstanceID(
                "12345678-9abc-4def-\(variant)abc-123456789abc"))
        }
        let invalidIDs: [String?] = [
            nil, "", "not-an-id", instanceID.uppercased(), " \(instanceID)", "\(instanceID)\n",
            "12345678-9abc-1def-8abc-123456789abc", "12345678-9abc-4def-7abc-123456789abc",
            "12345678-9abc-4def-cabc-123456789abc", "12345678-9abc-4def-8abc-123456789abg",
            "123456789abc4def8abc123456789abc", "12345678_9abc-4def-8abc-123456789abc",
            "12345678-9abc-4def-8abc-123456789ab", "12345678-9abc-4def-8abc-123456789abcd",
        ]
        for value in invalidIDs {
            XCTAssertFalse(RecordingPermissionContract.isValidServerInstanceID(value), value ?? "nil")
        }
    }

    func testPermissionSupportRequiresBothVersionAndValidServerIdentity() {
        XCTAssertTrue(RecordingPermissionContract.supportsSetup("1.1.0"))
        XCTAssertFalse(RecordingPermissionContract.supportsSetup("1.1.0", serverInstanceID: nil))
        XCTAssertFalse(RecordingPermissionContract.supportsSetup("1.1.0", serverInstanceID: "invalid"))
        XCTAssertFalse(RecordingPermissionContract.supportsSetup("1.0.0", serverInstanceID: instanceID))
        XCTAssertFalse(RecordingPermissionContract.supportsSetup("2.0.0", serverInstanceID: instanceID))
        XCTAssertTrue(RecordingPermissionContract.supportsSetup("1.1.0", serverInstanceID: instanceID))
    }

    func testServerInfoRefreshNeverSendsNewInputBeforeFeatureDetection() {
        let unsupportedVersions: [String?] = [nil, "1.0.0", "2.0.0", "malformed"]
        for version in unsupportedVersions {
            XCTAssertEqual(
                RecordingPermissionContract.serverInfoArguments(
                    contractVersion: version, serverInstanceID: instanceID, refreshPermissions: true), [:])
        }
        XCTAssertEqual(
            RecordingPermissionContract.serverInfoArguments(contractVersion: "1.1.0", refreshPermissions: true), [:])
        XCTAssertEqual(
            RecordingPermissionContract.serverInfoArguments(
                contractVersion: "1.1.0", serverInstanceID: "invalid", refreshPermissions: true), [:])
        XCTAssertEqual(
            RecordingPermissionContract.serverInfoArguments(
                contractVersion: "1.1.0", serverInstanceID: instanceID, refreshPermissions: false), [:])
        XCTAssertEqual(
            RecordingPermissionContract.serverInfoArguments(
                contractVersion: "1.1.0", serverInstanceID: instanceID, refreshPermissions: true),
            ["refresh_permissions": true])
    }

    private func responseObject() -> [String: Any] {
        [
            "permission": "microphone", "action": "request", "settings_opened": false,
            "permissions": ["screen_recording": "denied", "microphone": "granted"],
        ]
    }

    private func capabilitiesObject() -> [String: Any] {
        [
            "recording": false, "transports": ["streamable-http"], "transcription_engines": ["fake"],
            "diarization_engines": ["none"], "llm_providers": ["none"], "export_destinations": ["markdown"],
        ]
    }

    private func decodeResponse(_ object: [String: Any]) throws -> ConfigureRecordingPermissionResponse {
        try JSONDecoder().decode(
            ConfigureRecordingPermissionResponse.self, from: JSONSerialization.data(withJSONObject: object))
    }

    private func decodeCapabilities(_ object: [String: Any]) throws -> ServerCapabilities {
        try JSONDecoder().decode(ServerCapabilities.self, from: JSONSerialization.data(withJSONObject: object))
    }
}
