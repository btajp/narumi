import AppKit
import Foundation
import NarumiMenuBarCore
import SwiftUI

/// Menu bar UI. An MCP client for every data operation; the only thing it does besides calling
/// tools is starting / stopping the server *process* through `ServerLauncher` and checking for
/// app updates through Sparkle.
@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    static var appVersion: String {
        Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "development"
    }
    static let displayName = "narumi"
    static let pollInterval: TimeInterval = 5

    private var statusItem: NSStatusItem!
    private var startItem: NSMenuItem!
    private var stopItem: NSMenuItem!
    private var serverItem: NSMenuItem!
    private var restartItem: NSMenuItem!
    private var chooseRepoItem: NSMenuItem!
    private var openLogItem: NSMenuItem!
    private var updateItem: NSMenuItem!
    private var pollTimer: Timer?
    private var statusRefreshTask: Task<Void, Never>?
    private var signalSources: [DispatchSourceSignal] = []
    private var session = DesktopSessionState()
    private var recording: Bool { session.recording.active }
    private var busy: Bool { session.operation != nil }
    private var terminating: Bool { session.terminating }
    private var knownJobsBusy = false
    private var lastJobRequestPublication: UInt64 = 0
    private var shutdownDone = false
    private var userRequestedQuit = false
    private var lastInfo: ServerInfoSummary?
    private var client: MCPClient!
    private var launcher: ServerLauncher!
    private var updater: DesktopUpdater!
    private var mainWindow: NSWindow?
    private var mainWindowModel: MainWindowModel?

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)
        let config = ServerConfig.resolve(
            storedRepoPath: UserDefaults.standard.string(forKey: ServerConfig.repoPathDefaultsKey))
        let client = MCPClient(
            serverURL: config.serverURL, clientVersion: AppDelegate.appVersion,
            jobRequestObserver: { [weak self] publication, count, pendingStop, jobIDs in
                guard let self, publication > self.lastJobRequestPublication else { return }
                self.lastJobRequestPublication = publication
                let model = self.ensureMainWindowModel()
                for jobID in jobIDs { model.track(jobID: jobID) }
                self.session.setJobRequestState(pending: count > 0, pendingStop: pendingStop)
                model.unresolvedJobRequestCount = count
                self.applyState()
            })
        self.client = client
        launcher = ServerLauncher(config: config, client: client)
        launcher.onStateChange = { [weak self] state in
            guard let self else {
                return
            }
            session.connectionChanged(to: state)
            lastInfo = nil
            applyServerState()
            if state.pollsServerInfo {
                Task { await self.refreshServerStatus() }
            }
        }

        updater = DesktopUpdater(
            blockReason: { [weak self] in
                guard let self else { return "アプリを準備中です" }
                return updateBlockReason
            },
            refreshSafety: { [weak self] in
                await self?.refreshServerStatus()
                await self?.mainWindowModel?.refreshJobs()
            },
            installationChanged: { [weak self] installing in
                self?.session.setInstallingUpdate(installing)
                self?.applyState()
            })

        statusItem = NSStatusBar.system.statusItem(withLength: 88)
        statusItem.button?.title = AppDelegate.displayName
        statusItem.button?.imagePosition = .imageLeading
        statusItem.menu = buildMenu()
        installSignalHandlers()
        applyServerState()
        openMainWindow()

        Task {
            await launcher.start()
            applyServerState()
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
        // An updater must never turn its Quit request into the normal "stop recording"
        // flow. A late active/unknown state cancels the relaunch without stopping anything.
        if session.shouldDeferUpdateTermination(
            updateOwnsTermination: updater?.ownsTermination == true,
            updateInstalling: updater?.installing == true, userRequestedQuit: userRequestedQuit,
            launcherBusy: launcher?.isBusy ?? true, knownJobsBusy: knownJobsBusy)
        {
            updater.installationTerminationDenied()
            return .terminateCancel
        }
        guard !busy else {
            if userRequestedQuit {
                presentMessage(
                    title: "録画の操作中です",
                    text: "録画の開始・停止が完了してから、もう一度「終了」を選んでください。")
            }
            return .terminateCancel
        }
        if recording && !confirmStopRecordingBeforeQuit() {
            return .terminateCancel
        }
        // isBusy covers a start in flight — notably a bundled-runtime sync, whose uv
        // subprocess would otherwise be orphaned; stop() cancels it and waits for the exit.
        let stopServer = launcher.managesProcess || launcher.isBusy
        ServerLauncher.stderr("narumi.app: quitting (recording=\(recording), managed server=\(stopServer))\n")
        guard recording || stopServer else {
            return .terminateNow
        }
        session.beginTermination()
        pollTimer?.invalidate()
        mainWindowModel?.stopPolling()
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
                    session.confirmStoppedForShutdown()
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
        let openItem = NSMenuItem(title: "narumi を開く", action: #selector(openMainWindow), keyEquivalent: "")
        openItem.target = self
        menu.addItem(openItem)
        menu.addItem(.separator())

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
        updateItem = NSMenuItem(
            title: "アップデートを確認…",
            action: #selector(checkForUpdates), keyEquivalent: "")
        updateItem.target = self
        menu.addItem(updateItem)

        menu.addItem(.separator())
        let quit = NSMenuItem(title: "終了", action: #selector(quit), keyEquivalent: "q")
        quit.target = self
        menu.addItem(quit)
        menu.autoenablesItems = false
        return menu
    }

    // MARK: Main window

    /// Open (or bring forward) the main window. The app normally lives as a menu bar
    /// accessory; while the window is open it becomes a regular app so the window can be
    /// focused, and `windowWillClose` returns it to `.accessory`.
    @objc func openMainWindow() {
        let model = ensureMainWindowModel()
        if mainWindow == nil {
            let window = NSWindow(
                contentRect: NSRect(x: 0, y: 0, width: 1080, height: 680),
                styleMask: [.titled, .closable, .miniaturizable, .resizable],
                backing: .buffered, defer: false)
            window.title = AppDelegate.displayName
            window.isReleasedWhenClosed = false
            window.contentView = NSHostingView(rootView: MainWindowView(model: model))
            window.center()
            window.setFrameAutosaveName("NarumiMainWindow")
            window.delegate = self
            mainWindow = window
        }
        NSApp.setActivationPolicy(.regular)
        NSApp.activate(ignoringOtherApps: true)
        mainWindow?.makeKeyAndOrderFront(nil)
        model.startPolling()
    }

    private func ensureMainWindowModel() -> MainWindowModel {
        if let mainWindowModel {
            return mainWindowModel
        }
        let model = MainWindowModel(client: NarumiClient(mcp: client))
        model.hostActions = MainWindowModel.HostActions(
            restartServer: { [weak self] in self?.restartServer() },
            openServerLog: { [weak self] in self?.openLog() },
            checkForUpdates: { [weak self] in self?.checkForUpdates() },
            startRecording: { [weak self] in self?.startRecording() },
            stopRecording: { [weak self] in self?.stopRecording() },
            jobActivityChanged: { [weak self] active in
                self?.knownJobsBusy = active
                self?.applyState()
            })
        mainWindowModel = model
        model.applyDesktopSession(session)
        return model
    }

    /// Clicking the Dock icon while the window exists but is closed reopens it.
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows: Bool) -> Bool {
        if !hasVisibleWindows {
            openMainWindow()
        }
        return true
    }

    private func applyState() {
        guard statusItem != nil else { return }
        let image = NSImage(systemSymbolName: session.menuSymbolName, accessibilityDescription: session.accessibilityLabel)
        image?.isTemplate = true
        image?.size = NSSize(width: 18, height: 18)
        statusItem.button?.image = image
        statusItem.button?.toolTip = session.accessibilityLabel
        statusItem.button?.setAccessibilityLabel(session.accessibilityLabel)
        if recording, let elapsed = session.recording.elapsedSec {
            stopItem.title = "録画停止（\(NarumiFormat.duration(elapsed))）"
        } else {
            stopItem.title = "録画停止"
        }
        startItem.isEnabled = session.canStart
        stopItem.isEnabled = session.canStop
        updateItem.isEnabled = updater?.canCheckForUpdates == true
        mainWindowModel?.applyDesktopSession(session)
        updater?.stateDidChange()
    }

    private func applyServerState() {
        guard let launcher else {
            return
        }
        let state = launcher.state
        serverItem.title = ServerStatusText.title(for: state, info: lastInfo, reachable: session.serverReachable)
        serverItem.toolTip = state.detail
        restartItem.isEnabled = state.canRestart && !launcher.isBusy && !busy && !terminating && !session.installingUpdate
        chooseRepoItem.isEnabled = !launcher.isBusy && !busy && !recording && !terminating && !session.installingUpdate
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
        guard let token = session.beginStart() else {
            return
        }
        applyState()
        guard let options = promptForRecordingOptions() else {
            session.cancelStart(token)
            applyState()
            return
        }
        Task {
            guard session.isCurrentOperation(token) else {
                presentMessage(
                    title: "録画は開始していません",
                    text: "接続が切り替わったため、開始要求を送信しませんでした。準備が整ってからもう一度「録画開始」を選んでください。")
                return
            }
            defer {
                applyState()
                Task { await refreshServerStatus() }
            }
            do {
                var arguments: [String: JSONNode] = [
                    "meeting_name": .string(options.meetingName),
                    "request_id": .string(UUID().uuidString),
                ]
                if let profile = options.profile {
                    arguments["profile"] = .string(profile)
                }
                if let scope = options.scope {
                    arguments["scope"] = .string(scope)
                }
                let result = try await client.callTool(ToolCatalog.startRecording, arguments: arguments)
                let accepted = session.finishStart(
                    token,
                    recording: RecordingStatus(
                        active: true,
                        meetingID: result.structuredContent?["meeting_id"]?.stringValue,
                        meetingName: options.meetingName,
                        startedAt: result.structuredContent?["started_at"]?.stringValue,
                        elapsedSec: 0))
                if accepted {
                    mainWindowModel?.showToast("録画を開始しました")
                } else {
                    mainWindowModel?.showToast("接続が切り替わったため、現在の録画状態を再確認しています")
                }
            } catch {
                if session.failOperation(token) {
                    presentError(title: "録画を開始できません", error: error)
                }
            }
        }
    }

    @objc private func stopRecording() {
        guard let token = session.beginStop() else {
            return
        }
        applyState()
        Task {
            guard session.isCurrentOperation(token) else { return }
            defer {
                applyState()
                Task { await refreshServerStatus() }
            }
            // stop_recording's contract takes only request_id (+ auto_process / discard_video);
            // the server tracks the single running recording itself.
            do {
                let result = try await client.callTool(ToolCatalog.stopRecording, arguments: [
                    "request_id": .string(UUID().uuidString),
                ])
                if session.finishStop(token) {
                    if let jobID = result.structuredContent?["job_id"]?.stringValue {
                        ensureMainWindowModel().track(jobID: jobID)
                    }
                    mainWindowModel?.showToast("録画を停止して保存しました")
                    await mainWindowModel?.refresh()
                }
            } catch {
                if session.failOperation(token) {
                    presentError(title: "録画を停止できません", error: error)
                }
            }
        }
    }

    // MARK: Server actions

    @objc private func restartServer() {
        guard launcher.state.canRestart, !launcher.isBusy, !busy, !terminating, !session.installingUpdate else {
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
        }
        session.connectionChanged(to: .starting(since: Date()))
        applyServerState()
        Task {
            await launcher.restart()
            applyServerState()
            await refreshServerStatus()
        }
    }

    @objc private func chooseRepository() {
        guard !launcher.isBusy, !busy, !recording, !terminating, !session.installingUpdate else {
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
            session.connectionChanged(to: .starting(since: Date()))
            applyState()
            await launcher.restart()
            applyServerState()
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
        // A menu-selected Quit is an explicit user action, unlike Sparkle's otherwise
        // indistinguishable OS Quit request. It retains the normal recording confirmation.
        userRequestedQuit = true
        defer { userRequestedQuit = false }
        NSApp.terminate(nil)
    }

    private var updateBlockReason: String? {
        session.updateBlockReason(launcherBusy: launcher?.isBusy ?? true, knownJobsBusy: knownJobsBusy)
    }

    @objc private func checkForUpdates() {
        if let reason = updateBlockReason {
            mainWindowModel?.showToast(reason)
            return
        }
        updater.checkForUpdates()
    }

    // MARK: Server status

    private func refreshServerStatus() async {
        if let statusRefreshTask {
            await statusRefreshTask.value
            return
        }
        let task = Task { await performServerStatusRefresh() }
        statusRefreshTask = task
        await task.value
        if statusRefreshTask == task {
            statusRefreshTask = nil
        }
    }

    private func performServerStatusRefresh() async {
        guard let launcher, launcher.state.pollsServerInfo, let token = session.beginPoll() else {
            applyServerState()
            return
        }
        do {
            let result = try await client.callTool(ToolCatalog.getServerInfo, arguments: [:])
            guard session.isCurrentPoll(token) else { return }
            let info = ServerInfoSummary(node: result.structuredContent)
            let status = try await NarumiClient(mcp: client).recordingStatus()
            guard session.finishPoll(token, info: info, recording: status) else { return }
            lastInfo = info
        } catch {
            guard session.failPoll(token) else { return }
            applyServerState()
            await client.reset()
        }
        applyServerState()
        if session.serverReachable {
            // Continue tracking jobs after the window closes, so a deferred update can
            // resume when transcription/export has finished.
            await client.recoverPendingJobCalls()
            await mainWindowModel?.refreshJobs()
        }
    }

    // MARK: Dialogs

    private struct RecordingOptions {
        var meetingName: String
        var profile: String?
        var scope: String?
    }

    /// Start dialog: 会議名 (required), プロファイル and scope (optional; empty = server-side
    /// default profile / unscoped).
    private func promptForRecordingOptions() -> RecordingOptions? {
        NSApp.activate(ignoringOtherApps: true)
        let alert = NSAlert()
        alert.messageText = "録画を開始します"
        alert.informativeText = "会議名を入力してください。プロファイル・scope は空なら既定（既定プロファイル / scope なし）です。"
        alert.addButton(withTitle: "録画開始")
        alert.addButton(withTitle: "キャンセル")

        let nameField = NSTextField(frame: NSRect(x: 0, y: 56, width: 280, height: 24))
        nameField.placeholderString = "会議名"
        let profileField = NSTextField(frame: NSRect(x: 0, y: 28, width: 280, height: 24))
        profileField.placeholderString = "プロファイル（空 = 既定）"
        let scopeField = NSTextField(frame: NSRect(x: 0, y: 0, width: 280, height: 24))
        scopeField.placeholderString = "scope（空 = scope なし）"
        nameField.nextKeyView = profileField
        profileField.nextKeyView = scopeField
        scopeField.nextKeyView = nameField
        let accessory = NSView(frame: NSRect(x: 0, y: 0, width: 280, height: 80))
        accessory.addSubview(nameField)
        accessory.addSubview(profileField)
        accessory.addSubview(scopeField)
        alert.accessoryView = accessory
        alert.window.initialFirstResponder = nameField

        guard alert.runModal() == .alertFirstButtonReturn else {
            return nil
        }
        let name = nameField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !name.isEmpty else {
            return nil
        }
        let profile = profileField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        let scope = scopeField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        return RecordingOptions(
            meetingName: name,
            profile: profile.isEmpty ? nil : profile,
            scope: scope.isEmpty ? nil : scope)
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

extension AppDelegate: NSWindowDelegate {
    /// Closing the main window stops the 5 s refresh loop and returns the app to a pure
    /// menu bar accessory (no Dock icon). The window itself is kept for the next open.
    func windowWillClose(_ notification: Notification) {
        guard let closing = notification.object as? NSWindow, closing === mainWindow else {
            return
        }
        mainWindowModel?.stopPolling()
        NSApp.setActivationPolicy(.accessory)
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
