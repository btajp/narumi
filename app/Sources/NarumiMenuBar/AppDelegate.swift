import AppKit
import Foundation
import NarumiMenuBarCore

/// Menu bar UI. An MCP client for every data operation; the only thing it does besides calling
/// tools is starting / stopping the server *process* through `ServerLauncher`.
@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    static let appVersion = "0.1.0"
    static let displayName = "narumi"
    static let idleIcon = "🕵️"
    static let recordingIcon = "⏺"
    static let pollInterval: TimeInterval = 5

    private var statusItem: NSStatusItem!
    private var startItem: NSMenuItem!
    private var stopItem: NSMenuItem!
    private var serverItem: NSMenuItem!
    private var restartItem: NSMenuItem!
    private var chooseRepoItem: NSMenuItem!
    private var openLogItem: NSMenuItem!
    private var pollTimer: Timer?
    private var signalSources: [DispatchSourceSignal] = []
    private var recording = false
    private var busy = false
    private var terminating = false
    private var shutdownDone = false
    private var currentMeetingID: String?
    private var lastInfo: ServerInfoSummary?
    private var serverReachable = false
    private var client: MCPClient!
    private var launcher: ServerLauncher!

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)
        let config = ServerConfig.resolve(
            storedRepoPath: UserDefaults.standard.string(forKey: ServerConfig.repoPathDefaultsKey))
        let client = MCPClient(serverURL: config.serverURL, clientVersion: AppDelegate.appVersion)
        self.client = client
        launcher = ServerLauncher(config: config, client: client)
        launcher.onStateChange = { [weak self] state in
            guard let self else {
                return
            }
            if state.pollsServerInfo {
                serverReachable = true  // the launcher just got an answer
                Task { await self.refreshServerStatus() }
            }
            applyServerState()
        }

        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        statusItem.button?.title = AppDelegate.idleIcon
        statusItem.button?.toolTip = AppDelegate.displayName
        statusItem.menu = buildMenu()
        installSignalHandlers()
        applyServerState()

        Task {
            await launcher.start()
        }
        pollTimer = Timer.scheduledTimer(withTimeInterval: AppDelegate.pollInterval, repeats: true) { [weak self] _ in
            Task { @MainActor in
                await self?.refreshServerStatus()
            }
        }
    }

    func applicationWillTerminate(_ notification: Notification) {
        pollTimer?.invalidate()
    }

    /// Quit: stop a recording this client started (after confirmation), then stop the server
    /// we launched. An external server is left alone.
    ///
    /// The asynchronous part runs while this method pumps the default run-loop mode, and the
    /// answer is `.terminateNow` once it is done. `.terminateLater` is deliberately not used:
    /// while AppKit waits for `reply(toApplicationShouldTerminate:)` no main-actor job was ever
    /// run (observed on macOS 26), which left the server orphaned.
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        if terminating {
            return .terminateCancel  // one shutdown is already in progress
        }
        if recording && !confirmStopRecordingBeforeQuit() {
            return .terminateCancel
        }
        let stopServer = launcher.managesProcess
        ServerLauncher.stderr("narumi.app: quitting (recording=\(recording), managed server=\(stopServer))\n")
        guard recording || stopServer else {
            return .terminateNow
        }
        terminating = true
        pollTimer?.invalidate()
        applyState()
        applyServerState()
        shutdownDone = false
        Task {
            if recording {
                do {
                    // A managed server is stopped right after this call, so don't enqueue a
                    // process job it can never finish (ctx.close would wait on it until our
                    // SIGKILL cuts it mid-transcription). An external server stays alive and
                    // runs the job as usual.
                    _ = try await client.callTool(ToolCatalog.stopRecording, arguments: [
                        "request_id": .string(UUID().uuidString),
                        "auto_process": .bool(!stopServer),
                    ])
                    recording = false
                    currentMeetingID = nil
                } catch {
                    // The managed server finalizes the recording itself at shutdown (without the
                    // process job); an external one does not, so the user must know.
                    presentError(title: "録画を停止できませんでした", error: error)
                }
            }
            if stopServer {
                await launcher.stop()
            }
            shutdownDone = true
        }
        while !shutdownDone {
            // Default mode is a common mode, so main-actor jobs (the Task above, the launcher's
            // sleeps, URLSession completions) are serviced here.
            if !RunLoop.main.run(mode: .default, before: Date(timeIntervalSinceNow: 0.1)) {
                Thread.sleep(forTimeInterval: 0.01)
            }
        }
        ServerLauncher.stderr("narumi.app: shutdown complete\n")
        return .terminateNow
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
        restartItem = NSMenuItem(title: "サーバーを再起動", action: #selector(restartServer), keyEquivalent: "")
        restartItem.target = self
        menu.addItem(restartItem)
        chooseRepoItem = NSMenuItem(title: "リポジトリを選択…", action: #selector(chooseRepository), keyEquivalent: "")
        chooseRepoItem.target = self
        menu.addItem(chooseRepoItem)
        openLogItem = NSMenuItem(title: "ログを開く", action: #selector(openLog), keyEquivalent: "")
        openLogItem.target = self
        menu.addItem(openLogItem)

        menu.addItem(.separator())
        let quit = NSMenuItem(title: "終了", action: #selector(quit), keyEquivalent: "q")
        quit.target = self
        menu.addItem(quit)
        menu.autoenablesItems = false
        return menu
    }

    private func applyState() {
        statusItem.button?.title = recording ? AppDelegate.recordingIcon : AppDelegate.idleIcon
        let serving = launcher?.state.pollsServerInfo ?? false
        startItem.isEnabled = !recording && !busy && !terminating && serving
        stopItem.isEnabled = recording && !busy && !terminating
    }

    private func applyServerState() {
        guard let launcher else {
            return
        }
        let state = launcher.state
        serverItem.title = ServerStatusText.title(for: state, info: lastInfo, reachable: serverReachable)
        serverItem.toolTip = state.detail
        restartItem.isEnabled = state.canRestart && !launcher.isBusy && !terminating
        chooseRepoItem.isEnabled = !launcher.isBusy && !terminating
        applyState()
    }

    /// SIGTERM / SIGINT (e.g. `kill`, Ctrl-C when run from a terminal) quit through the normal
    /// path so the managed server is stopped instead of orphaned.
    private func installSignalHandlers() {
        for number in [SIGTERM, SIGINT] {
            signal(number, SIG_IGN)
            let source = DispatchSource.makeSignalSource(signal: number, queue: .main)
            source.setEventHandler {
                ServerLauncher.stderr("narumi.app: signal \(number) received; terminating\n")
                // This handler is a GCD main-queue block. A nested run loop started from inside
                // such a block cannot drain the main queue (CFRunLoop skips it while a main-queue
                // block is executing), and the shutdown in applicationShouldTerminate relies on
                // main-actor jobs — so hop onto the run loop proper first.
                RunLoop.main.perform {
                    MainActor.assumeIsolated {
                        NSApp.terminate(nil)
                    }
                }
            }
            source.resume()
            signalSources.append(source)
        }
    }

    // MARK: Recording actions

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
                let result = try await client.callTool(ToolCatalog.startRecording, arguments: [
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
                _ = try await client.callTool(ToolCatalog.stopRecording, arguments: [
                    "request_id": .string(UUID().uuidString),
                ])
                recording = false
                currentMeetingID = nil
            } catch {
                presentError(title: "録画を停止できません", error: error)
            }
        }
    }

    // MARK: Server actions

    @objc private func restartServer() {
        guard launcher.state.canRestart, !launcher.isBusy, !terminating else {
            return
        }
        if recording {
            guard confirm(
                message: "録画中です。サーバーを再起動すると録画は停止されます。",
                informative: "録画は停止時に確定されますが、自動処理は行われません。続行しますか？",
                confirmTitle: "再起動する")
            else {
                return
            }
            recording = false  // the server finalizes it at shutdown
            currentMeetingID = nil
        }
        applyServerState()
        Task {
            await launcher.restart()
            await refreshServerStatus()
        }
    }

    @objc private func chooseRepository() {
        guard !launcher.isBusy, !terminating else {
            return
        }
        NSApp.activate(ignoringOtherApps: true)
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.allowsMultipleSelection = false
        panel.canCreateDirectories = false
        panel.prompt = "選択"
        panel.message = "narumi リポジトリ（pyproject.toml と server/pyproject.toml があるディレクトリ）を選択してください"
        if let current = launcher.config.repository {
            panel.directoryURL = current
        }
        guard panel.runModal() == .OK, let url = panel.url else {
            return
        }
        guard ServerConfig.isRepository(url) else {
            presentMessage(
                title: "narumi リポジトリではありません",
                text: "\(url.path) に \(ServerConfig.repositoryMarkers.joined(separator: " と ")) が見つかりません。")
            return
        }
        UserDefaults.standard.set(url.path, forKey: ServerConfig.repoPathDefaultsKey)
        launcher.config = ServerConfig.resolve(storedRepoPath: url.path)
        if launcher.config.repositorySource == .environment {
            presentMessage(
                title: "環境変数 NARUMI_REPO が優先されます",
                text: "選択したリポジトリは保存しましたが、NARUMI_REPO=\(launcher.config.repository?.path ?? "") が設定されている間はそちらが使われます。")
        }
        Task {
            await launcher.restart()
            await refreshServerStatus()
        }
    }

    @objc private func openLog() {
        let url = launcher.config.logFile
        do {
            try ServerLauncher.prepareLogFile(at: url)
        } catch {
            presentError(title: "ログファイルを作成できません", error: error)
            return
        }
        NSWorkspace.shared.open(url)
    }

    @objc private func quit() {
        NSApp.terminate(nil)
    }

    // MARK: Server status

    private func refreshServerStatus() async {
        guard let launcher, launcher.state.pollsServerInfo, !terminating else {
            applyServerState()
            return
        }
        do {
            let result = try await client.callTool(ToolCatalog.getServerInfo, arguments: [:])
            lastInfo = ServerInfoSummary(node: result.structuredContent)
            serverReachable = true
        } catch {
            serverReachable = false
            await client.reset()
        }
        applyServerState()
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

    private func confirmStopRecordingBeforeQuit() -> Bool {
        confirm(
            message: "録画中です。停止してから終了しますか？",
            informative: "録画を停止して確定してから、サーバーを停止して終了します。",
            confirmTitle: "停止して終了")
    }

    private func confirm(message: String, informative: String, confirmTitle: String) -> Bool {
        NSApp.activate(ignoringOtherApps: true)
        let alert = NSAlert()
        alert.alertStyle = .warning
        alert.messageText = message
        alert.informativeText = informative
        alert.addButton(withTitle: confirmTitle)
        alert.addButton(withTitle: "キャンセル")
        return alert.runModal() == .alertFirstButtonReturn
    }

    private func presentMessage(title: String, text: String) {
        NSApp.activate(ignoringOtherApps: true)
        let alert = NSAlert()
        alert.alertStyle = .warning
        alert.messageText = title
        alert.informativeText = text
        alert.addButton(withTitle: "OK")
        alert.runModal()
    }

    private func presentError(title: String, error: any Error) {
        if let clientError = error as? MCPClientError {
            var text = clientError.description
            if case .tool(_, let payload?) = clientError {
                text += "\n\n" + payload.pretty()
            }
            presentMessage(title: title, text: text)
        } else {
            presentMessage(title: title, text: error.localizedDescription)
        }
    }
}

extension ServerInfoSummary {
    /// From `get_server_info`'s structured content.
    init(node: JSONNode?) {
        self.init(
            version: node?["server_version"]?.stringValue,
            contractVersion: node?["contract_version"]?.stringValue,
            recordingCapable: node?["capabilities"]?["recording"]?.boolValue)
    }
}
