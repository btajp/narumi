import Foundation
import NarumiMenuBarCore

extension MainWindowModel {
    /// The AppDelegate owns recording and permission operations for both UI surfaces.
    struct HostActions {
        var restartServer: () -> Void = {}
        var openServerLog: () -> Void = {}
        var checkForUpdates: () -> Void = {}
        var startRecording: () -> Void = {}
        var stopRecording: () -> Void = {}
        var jobActivityChanged: (Bool) -> Void = { _ in }
        var configureRecordingPermission: (RecordingPermission, RecordingPermissionAction) -> Void = { _, _ in }
        var refreshRecordingPermissions: () async -> Void = {}
    }

    var canOpenPermissionSetup: Bool {
        !desktopSession.recording.active && desktopSession.operation == nil
            && !desktopSession.installingUpdate && !desktopSession.terminating
    }

    var permissionSetupButtonTitle: String {
        permissionSetup.blocked ? "録画の権限を確認" : "録画の権限を設定"
    }

    var permissionProcessControlsBlocked: Bool {
        if permissionSetup.blocked || desktopSession.operation != nil
            || desktopSession.installingUpdate || desktopSession.terminating { return true }
        switch desktopSession.serverState {
        case .preparing, .starting: return true
        default: return false
        }
    }

    func openPermissionSetup() {
        guard canOpenPermissionSetup else { return }
        showDiagnosticsSheet = true
    }

    func configureRecordingPermission(_ permission: RecordingPermission, action: RecordingPermissionAction) {
        hostActions.configureRecordingPermission(permission, action)
    }

    func refreshRecordingPermissions() async {
        guard !refreshingPermissions else { return }
        refreshingPermissions = true
        defer { refreshingPermissions = false }
        await hostActions.refreshRecordingPermissions()
    }
}

extension RecordingPermission {
    var displayName: String {
        switch self {
        case .microphone: return "マイク"
        case .screenRecording: return "画面収録"
        }
    }

    var systemSymbol: String {
        switch self {
        case .microphone: return "mic"
        case .screenRecording: return "display"
        }
    }

    var purpose: String {
        switch self {
        case .microphone: return "自分の声を、システム音声とは別のトラックで録音するために必要です。"
        case .screenRecording: return "会議の画面とシステム音声を記録するために必要です。"
        }
    }

    var settingsPath: String {
        "システム設定 → プライバシーとセキュリティ → \(displayName)"
    }
}
