import AVFoundation
import CoreGraphics
import Foundation

public enum PermissionStatus: String, Codable, Sendable {
    case granted
    case denied
    case unknown
}

/// Output of `narumi-recorder check`.
public struct PermissionReport: Equatable, Sendable {
    public var screenRecording: PermissionStatus
    public var microphone: PermissionStatus

    public init(screenRecording: PermissionStatus, microphone: PermissionStatus) {
        self.screenRecording = screenRecording
        self.microphone = microphone
    }

    public func json() -> JSONValue {
        .obj([
            "screen_recording": .string(screenRecording.rawValue),
            "microphone": .string(microphone.rawValue),
        ])
    }

    public func serialized() -> String {
        json().serialized()
    }
}

public enum Permissions {
    /// Read-only status check. Does not prompt.
    ///
    /// Screen recording has no "not determined" query in CoreGraphics: `denied` therefore also
    /// covers "never asked". The first `record` run triggers the system prompt.
    public static func check() -> PermissionReport {
        let screen: PermissionStatus = CGPreflightScreenCaptureAccess() ? .granted : .denied
        return PermissionReport(screenRecording: screen, microphone: microphoneStatus())
    }

    public static func microphoneStatus() -> PermissionStatus {
        switch AVCaptureDevice.authorizationStatus(for: .audio) {
        case .authorized: return .granted
        case .denied, .restricted: return .denied
        case .notDetermined: return .unknown
        @unknown default: return .unknown
        }
    }

    /// Prompt for microphone access when undetermined; returns the final status.
    public static func requestMicrophoneAccess() async -> PermissionStatus {
        if AVCaptureDevice.authorizationStatus(for: .audio) == .notDetermined {
            _ = await AVCaptureDevice.requestAccess(for: .audio)
        }
        return microphoneStatus()
    }

    /// Prompt for screen recording access (the system shows the dialog once per binary).
    public static func requestScreenRecordingAccess() -> PermissionStatus {
        if CGPreflightScreenCaptureAccess() {
            return .granted
        }
        return CGRequestScreenCaptureAccess() ? .granted : .denied
    }
}
