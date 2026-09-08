import AppKit
import Foundation

extension MainWindowModel {
    /// File exporters use a save panel; remote destinations return their own reference.
    static func fileExtension(forDestination name: String) -> String? {
        switch name {
        case "markdown": return "md"
        case "html": return "html"
        default: return nil
        }
    }

    /// Reveal a tool-returned absolute path in Finder (allowed non-MCP convenience).
    func revealRef(_ ref: String) {
        guard ref.hasPrefix("/"), FileManager.default.fileExists(atPath: ref) else { return }
        NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: ref)])
    }

    func openBundleInFinder() {
        guard let path = detail?.bundlePath else { return }
        NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: path)])
    }

    static func isPlaybackFileAvailable(_ path: String) -> Bool {
        guard path.hasPrefix("/") else { return false }
        var isDirectory: ObjCBool = false
        return FileManager.default.fileExists(atPath: path, isDirectory: &isDirectory) && !isDirectory.boolValue
    }

    /// Playback opens only the absolute artifact path returned by get_meeting.
    func playRecording() async {
        guard let path = detail?.playback?.path else { return }
        guard Self.isPlaybackFileAvailable(path) else {
            await loadDetail()
            showToast("録画ファイルが見つかりません。「録画ファイルを生成」から作り直せます。")
            return
        }
        if !NSWorkspace.shared.open(URL(fileURLWithPath: path)) {
            alert = AlertContent(title: "録画を再生できません", message: "録画ファイルを開くアプリを起動できませんでした。")
        }
    }
}
