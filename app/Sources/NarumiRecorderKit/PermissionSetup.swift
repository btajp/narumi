import AppKit
import Foundation

public enum RecordingPermission: String, Codable, CaseIterable, Sendable {
    case microphone
    case screenRecording = "screen_recording"
}

public enum PermissionSetupAction: String, Codable, Sendable {
    case request
    case openSettings = "open_settings"
}

/// A permission operation result, not a recording event or a permission grant.
public struct PermissionSetupResult: Equatable, Sendable {
    public let permission: RecordingPermission
    public let action: PermissionSetupAction
    public let permissions: PermissionReport
    public let settingsOpened: Bool

    public func serialized() -> String {
        JSONValue.obj([
            "permission": .string(permission.rawValue),
            "action": .string(action.rawValue),
            "permissions": permissions.json(),
            "settings_opened": .bool(settingsOpened),
        ]).serialized()
    }
}

/// The narrow OS boundary keeps permission setup tests free of TCC and capture side effects.
@MainActor
public protocol PermissionSetupDriver {
    func check() -> PermissionReport
    func requestMicrophoneAccess() async
    func requestScreenRecordingAccess()
    func openSettingsURL(_ url: URL) -> Bool
}

/// Handles only the two permission commands. It never constructs a capture session or writers.
@MainActor
public enum PermissionSetup {
    static let privacySettingsURL = URL(
        string: "x-apple.systempreferences:com.apple.preference.security")!

    public static func run(_ command: RecorderCommand) async throws -> PermissionSetupResult {
        try await run(command, using: SystemPermissionSetupDriver())
    }

    public static func run(
        _ command: RecorderCommand, using driver: any PermissionSetupDriver
    ) async throws -> PermissionSetupResult {
        let permission: RecordingPermission
        let action: PermissionSetupAction
        switch command {
        case .requestPermission(let target):
            permission = target
            action = .request
        case .openPermissionSettings(let target):
            permission = target
            action = .openSettings
        default:
            throw RecorderError(.invalidArgument, "permission setup requires a permission command")
        }

        var settingsOpened = false
        switch action {
        case .request:
            let before = driver.check()
            switch permission {
            case .microphone:
                // macOS does not re-prompt after a denial; the Settings action is separate.
                if before.microphone == .unknown {
                    await driver.requestMicrophoneAccess()
                }
            case .screenRecording:
                // CoreGraphics cannot distinguish a denial from a never-requested state.
                if before.screenRecording != .granted {
                    driver.requestScreenRecordingAccess()
                }
            }
        case .openSettings:
            settingsOpened = driver.openSettingsURL(settingsURL(for: permission))
            if !settingsOpened {
                settingsOpened = driver.openSettingsURL(privacySettingsURL)
            }
            guard settingsOpened else {
                throw RecorderError(.captureFailed, "cannot open macOS privacy settings")
            }
        }

        // A callback or Settings launch is not proof of permission. Always return a fresh check.
        return PermissionSetupResult(
            permission: permission, action: action,
            permissions: driver.check(), settingsOpened: settingsOpened)
    }

    static func settingsURL(for permission: RecordingPermission) -> URL {
        switch permission {
        case .microphone:
            return URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone")!
        case .screenRecording:
            return URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture")!
        }
    }
}

@MainActor
private struct SystemPermissionSetupDriver: PermissionSetupDriver {
    func check() -> PermissionReport {
        Permissions.check()
    }

    func requestMicrophoneAccess() async {
        _ = await Permissions.requestMicrophoneAccess()
    }

    func requestScreenRecordingAccess() {
        _ = Permissions.requestScreenRecordingAccess()
    }

    func openSettingsURL(_ url: URL) -> Bool {
        NSWorkspace.shared.open(url)
    }
}
