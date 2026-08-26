import AppKit
import Foundation

/// Menu bar UI. Only an MCP client: it never touches files or spawns the recorder itself.
@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    static let appVersion = "0.1.0"
    static let displayName = "鳴海探偵事務所"
    static let idleIcon = "🕵️"
    static let recordingIcon = "⏺"
    static let pollInterval: TimeInterval = 5

    private var statusItem: NSStatusItem!
    private var startItem: NSMenuItem!
    private var stopItem: NSMenuItem!
    private var serverItem: NSMenuItem!
    private var pollTimer: Timer?
    private var recording = false
    private var busy = false
    private var currentMeetingID: String?
    private let client = MCPClient(serverURL: MCPClient.serverURLFromEnvironment(), clientVersion: AppDelegate.appVersion)

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        statusItem.button?.title = AppDelegate.idleIcon
        statusItem.button?.toolTip = AppDelegate.displayName
        statusItem.menu = buildMenu()

        pollTimer = Timer.scheduledTimer(withTimeInterval: AppDelegate.pollInterval, repeats: true) { [weak self] _ in
            Task { @MainActor in
                await self?.refreshServerStatus()
            }
        }
        Task {
            await refreshServerStatus()
        }
    }

    func applicationWillTerminate(_ notification: Notification) {
        pollTimer?.invalidate()
    }

    // MARK: Menu

    private func buildMenu() -> NSMenu {
        let menu = NSMenu()
        startItem = NSMenuItem(title: "録画開始…", action: #selector(startRecording), keyEquivalent: "")
        startItem.target = self
        menu.addItem(startItem)

        stopItem = NSMenuItem(title: "録画停止", action: #selector(stopRecording), keyEquivalent: "")
        stopItem.target = self
        stopItem.isEnabled = false
        menu.addItem(stopItem)

        menu.addItem(.separator())
        serverItem = NSMenuItem(title: "サーバー: 確認中…", action: nil, keyEquivalent: "")
        serverItem.isEnabled = false
        menu.addItem(serverItem)

        menu.addItem(.separator())
        let quit = NSMenuItem(title: "終了", action: #selector(quit), keyEquivalent: "q")
        quit.target = self
        menu.addItem(quit)
        menu.autoenablesItems = false
        return menu
    }

    private func applyState() {
        statusItem.button?.title = recording ? AppDelegate.recordingIcon : AppDelegate.idleIcon
        startItem.isEnabled = !recording && !busy
        stopItem.isEnabled = recording && !busy
    }

    // MARK: Actions

    @objc private func startRecording() {
        guard !recording, !busy else {
            return
        }
        guard let meetingName = promptForMeetingName() else {
            return
        }
        busy = true
        applyState()
        Task {
            defer {
                busy = false
                applyState()
            }
            do {
                let result = try await client.callTool("start_recording", arguments: [
                    "meeting_name": .string(meetingName),
                    "request_id": .string(UUID().uuidString),
                ])
                currentMeetingID = result.structuredContent?["meeting_id"]?.stringValue
                recording = true
            } catch {
                presentError(title: "録画を開始できません", error: error)
            }
        }
    }

    @objc private func stopRecording() {
        guard recording, !busy else {
            return
        }
        busy = true
        applyState()
        Task {
            defer {
                busy = false
                applyState()
            }
            // stop_recording's contract takes only request_id (+ auto_process / discard_video);
            // the server tracks the single running recording itself.
            do {
                _ = try await client.callTool("stop_recording", arguments: [
                    "request_id": .string(UUID().uuidString),
                ])
                recording = false
                currentMeetingID = nil
            } catch {
                presentError(title: "録画を停止できません", error: error)
            }
        }
    }

    @objc private func quit() {
        NSApp.terminate(nil)
    }

    // MARK: Server status

    private func refreshServerStatus() async {
        do {
            let result = try await client.callTool("get_server_info", arguments: [:])
            let info = result.structuredContent
            var parts: [String] = ["稼働中"]
            if let version = info?["server_version"]?.stringValue {
                parts.append("v\(version)")
            }
            if let contract = info?["contract_version"]?.stringValue {
                parts.append("契約 \(contract)")
            }
            // capabilities.recording = "recording is possible on this machine" (recorder binary
            // found + permissions), not "a recording is running". The contract exposes no live
            // recording state, so the icon follows this client's own start/stop calls only.
            if info?["capabilities"]?["recording"]?.boolValue == false {
                parts.append("録画不可")
            }
            serverItem.title = "サーバー: " + parts.joined(separator: " ")
        } catch {
            serverItem.title = "サーバー: 未接続 (\(client.serverURL.absoluteString))"
            await client.reset()
        }
        applyState()
    }

    // MARK: Dialogs

    private func promptForMeetingName() -> String? {
        NSApp.activate(ignoringOtherApps: true)
        let alert = NSAlert()
        alert.messageText = "録画を開始します"
        alert.informativeText = "会議名を入力してください。"
        alert.addButton(withTitle: "録画開始")
        alert.addButton(withTitle: "キャンセル")
        let field = NSTextField(frame: NSRect(x: 0, y: 0, width: 280, height: 24))
        field.placeholderString = "会議名"
        alert.accessoryView = field
        alert.window.initialFirstResponder = field
        guard alert.runModal() == .alertFirstButtonReturn else {
            return nil
        }
        let name = field.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        return name.isEmpty ? nil : name
    }

    private func presentError(title: String, error: any Error) {
        NSApp.activate(ignoringOtherApps: true)
        let alert = NSAlert()
        alert.alertStyle = .warning
        alert.messageText = title
        if let clientError = error as? MCPClientError {
            var text = clientError.description
            if case .tool(_, let payload?) = clientError {
                text += "\n\n" + payload.pretty()
            }
            alert.informativeText = text
        } else {
            alert.informativeText = error.localizedDescription
        }
        alert.addButton(withTitle: "OK")
        alert.runModal()
    }
}
