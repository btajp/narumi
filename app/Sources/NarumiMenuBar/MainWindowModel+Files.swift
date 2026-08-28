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
}
