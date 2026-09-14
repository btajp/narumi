import AppKit
import Foundation
import NarumiMenuBarCore

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
        guard let detail, detail.meeting.meetingID == selectedMeetingID else { return }
        let path = detail.bundlePath
        NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: path)])
    }

    static func isPlaybackFileAvailable(_ path: String) -> Bool {
        RecordingPlayback.availableFileURL(at: path) != nil
    }

    /// Select the MP4 itself so it is immediately available for copying or sharing.
    func revealRecordingInFinder() async {
        let meetingID = selectedMeetingID
        guard let url = await playbackURLForFileAction(), selectedMeetingID == meetingID else { return }
        NSWorkspace.shared.activateFileViewerSelecting([url])
    }

    /// Playback opens only the absolute artifact path returned by get_meeting.
    func playRecording() async {
        let meetingID = selectedMeetingID
        guard let url = await playbackURLForFileAction(), selectedMeetingID == meetingID else { return }
        if !NSWorkspace.shared.open(url) {
            alert = AlertContent(title: "録画を再生できません", message: "録画ファイルを開くアプリを起動できませんでした。")
        }
    }

    private func playbackURLForFileAction() async -> URL? {
        guard let detail, detail.meeting.meetingID == selectedMeetingID else { return nil }
        if let path = detail.playback?.path, let url = RecordingPlayback.availableFileURL(at: path) {
            return url
        }
        await loadDetail()
        guard selectedMeetingID == detail.meeting.meetingID else { return nil }
        showToast("録画ファイルが見つかりません。録画ファイル欄で生成状態を確認してください。")
        return nil
    }
}
