import Foundation
import NarumiMenuBarCore

/// Starts and stops the `narumi-server` *process* so no Terminal is needed.
///
/// That is its only responsibility: every data operation still goes through `MCPClient`
/// (AGENTS.md 絶対原則 3). The launcher never reads bundles, the catalog or recordings; it only
/// spawns `ServerCommand`, redirects the output to the log file and signals the process.
@MainActor
final class ServerLauncher {
    static let startupTimeout: TimeInterval = 30
    static let startupPollInterval: Duration = .seconds(1)
    /// Covers the server's full ordered shutdown before we SIGKILL: uvicorn's graceful
    /// connection drain (`GRACEFUL_SHUTDOWN_TIMEOUT` 10 s — an open MCP GET stream uses all of
    /// it), then `ServerContext.close` finalizing a running recording (recorder `stop_timeout`
    /// 30 s + `EXIT_GRACE` 10 s) plus track hashing and the manifest write. 10+30+10 with margin.
    static let stopTimeout: TimeInterval = 60
    static let killGrace: TimeInterval = 5
    static let logRotateBytes: UInt64 = 5 * 1024 * 1024

    var config: ServerConfig
    private(set) var state: ServerState = .notConfigured {
        didSet {
            if state != oldValue {
                onStateChange?(state)
            }
        }
    }
    var onStateChange: ((ServerState) -> Void)?
    /// A process we spawned is alive (including a timed-out startup that is still running).
    var managesProcess: Bool { process?.isRunning == true || syncProcess?.isRunning == true }
    /// A start or stop is in flight (menu items that would race it are disabled meanwhile).
    var isBusy: Bool { startTask != nil || stopping }

    private let client: MCPClient
    private var process: Process?
    private var syncProcess: Process?
    private var processGroup: pid_t = 0
    private var logHandle: FileHandle?
    private var startTask: Task<Void, Never>?
    private var stopping = false
    private var stopRequested = false
    private var generation = 0
    private var runtimeInstallation: RuntimeInstallation?
    private var runtimeLease: RuntimeLease?
    private var runtimeSyncOwnership: RuntimeSyncOwnership?

    init(config: ServerConfig, client: MCPClient) {
        self.config = config
        self.client = client
    }

    // MARK: Public API

    /// Detect an external server, else spawn ours and wait until it answers `get_server_info`.
    /// A concurrent call joins the in-flight start; `stop()` cancels it.
    func start() async {
        if let startTask {
            await startTask.value
            return
        }
        let task = Task { @MainActor [weak self] in
            guard let self else {
                return
            }
            await self.performStart()
        }
        startTask = task
        await task.value
        if startTask == task {
            startTask = nil
        }
        if !managesProcess {
            runtimeLease = nil
            runtimeInstallation = nil
            runtimeSyncOwnership = nil
        }
    }

    /// SIGTERM, wait up to `timeout` for the graceful shutdown (recording finalization), then
    /// SIGKILL the whole process group (uv → python → recorder). Never leaves an orphan.
    func stop(timeout: TimeInterval = ServerLauncher.stopTimeout) async {
        if let startTask {
            startTask.cancel()
            await startTask.value
            self.startTask = nil
        }
        await stopManagedProcess(timeout: timeout)
        if !(await stopSyncProcess()) {
            state = .failed("環境準備プロセスを停止できません。再起動と環境の復旧を保留しました")
        } else if !managesProcess {
            if let recoveryError = recoverFailedInstallation() { state = .failed(recoveryError) }
            runtimeLease = nil
            runtimeInstallation = nil
            runtimeSyncOwnership = nil
        }
    }

    /// Separate from `stop()` so a failed start can stop its own child without awaiting the
    /// very start task it is running in. No unowned process is ever signalled here.
    private func stopManagedProcess(timeout: TimeInterval) async {
        guard let process else {
            return
        }
        stopping = true
        defer { stopping = false }
        stopRequested = true
        let pid = process.processIdentifier
        let group = processGroup
        note("stopping narumi-server (pid \(pid)): SIGTERM, waiting up to \(Int(timeout)) s")
        if process.isRunning {
            process.terminate()
        }
        var exited = await waitForExit(of: process, timeout: timeout)
        if !exited {
            note("no exit after \(Int(timeout)) s; SIGKILL to pid \(pid) / process group \(processGroup)")
            killTree(pid: pid, group: group)
            exited = await waitForExit(of: process, timeout: Self.killGrace)
        }
        if exited {
            process.waitUntilExit()  // already gone: reaps immediately
        } else {
            note("pid \(pid) is still alive after SIGKILL")
            state = .failed("起動したサーバーが停止しません。ランタイムの復旧を保留しました")
            return
        }
        // Belt and braces: nothing of the tree may survive even if uv died before the server.
        if group > 0, group != getpgrp(), killpg(group, 0) == 0 {
            note("process group \(group) still has members; SIGKILL")
            killpg(group, SIGKILL)
        }
        if self.process === process {
            finishProcess(exitCode: process.terminationStatus, signaled: process.terminationReason == .uncaughtSignal)
        }
    }

    func restart() async {
        await stop()
        await start()
    }

    /// Create the log file (and directory) so 「ログを開く」 has something to open.
    nonisolated static func prepareLogFile(at url: URL) throws {
        let fm = FileManager.default
        try fm.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        if !fm.fileExists(atPath: url.path) {
            guard fm.createFile(atPath: url.path, contents: nil) else {
                throw CocoaError(.fileWriteUnknown, userInfo: [NSFilePathErrorKey: url.path])
            }
        }
    }

    // MARK: Start

    private func performStart() async {
        if managesProcess {
            return
        }
        // A previously unresponsive child may have exited since the last attempt. Durable
        // sync ownership is checked again when acquiring a fresh lease below.
        runtimeLease = nil
        runtimeInstallation = nil
        runtimeSyncOwnership = nil
        syncProcess = nil
        do {
            if config.requiresOwnedServer {
                // Check before any sync/recovery, including when a stale server answers
                // perfectly valid MCP. Its process and runtime are not ours to change.
                try LocalServerPort.requireAvailable(config.port)
            } else if await serverInfo() != nil {
                try Task.checkCancellation()
                state = .external(config.serverURL)
                return
            }
            try Task.checkCancellation()
            guard let mode = config.runtimeMode else {
                state = .notConfigured
                return
            }
            let command: ServerCommand
            var identity: BundledServerIdentity?
            switch mode {
            case .repo:
                guard let repository = config.repository, let repoCommand = ServerCommand(config: config) else {
                    state = .notConfigured
                    return
                }
                guard ServerConfig.isRepository(repository) else {
                    throw StartupFailure(message: "narumi リポジトリではありません: \(repository.path)")
                }
                command = repoCommand
            case .bundled:
                guard let runtime = config.bundledRuntime, let bundledCommand = ServerCommand.bundled(config: config) else {
                    throw StartupFailure(message: "同梱ランタイムがありません（Resources/runtime）")
                }
                // Even explicit endpoints only permit adoption of an existing server; when
                // spawning our own, no other process may be listening on its bind port.
                try LocalServerPort.requireAvailable(config.port)
                runtimeLease = try RuntimeLease(paths: config.runtimePaths)
                runtimeSyncOwnership = RuntimeSyncOwnership(paths: config.runtimePaths)
                runtimeInstallation = RuntimeInstallation(paths: config.runtimePaths)
                identity = try await syncBundledRuntimeIfNeeded(runtime)
                command = bundledCommand
            }
            try Task.checkCancellation()
            if mode == .bundled {
                // Sync may take several minutes; check again immediately before launch.
                try LocalServerPort.requireAvailable(config.port)
                await client.reset()
            }
            let pid = try await launchAndWait(command, mode: mode, identity: identity)
            try Task.checkCancellation()
            guard let process, process.isRunning, process.processIdentifier == pid else {
                throw StartupFailure(message: "サーバーが更新確定前に終了しました（ログ参照）")
            }
            // Until this point installed.json still describes the old environment and its
            // venv is retained. A launch error must not mark the candidate as installed.
            try runtimeInstallation?.commit()
            state = .running(pid: pid)
            note("verified server at \(config.serverURL.absoluteString) (pid \(pid))")
        } catch is CancellationError {
            await stopManagedProcess(timeout: Self.stopTimeout)
            if let recoveryError = recoverFailedInstallation() {
                state = .failed(recoveryError)
            } else if process == nil {
                state = .stopped(exitCode: 0)
            }
        } catch {
            let message = error.localizedDescription
            note("startup failed: \(message)")
            if config.runtimeMode == .bundled {
                await stopManagedProcess(timeout: Self.stopTimeout)
            }
            let recoveryError = recoverFailedInstallation()
            state = .failed(message + (recoveryError.map { "\n" + $0 } ?? ""))
        }
    }

    private struct StartupFailure: LocalizedError {
        let message: String
        var errorDescription: String? { message }
    }

    /// Returns a pid only while the same owned process is alive and its response is valid.
    private func launchAndWait(
        _ command: ServerCommand, mode: ServerConfig.RuntimeMode, identity: BundledServerIdentity?
    ) async throws -> Int32 {
        let log: FileHandle
        do {
            log = try openLog(at: config.logFile)
        } catch {
            throw StartupFailure(message: "ログファイルを開けません: \(error.localizedDescription)")
        }
        logHandle = log
        note(
            "starting narumi-server (\(mode.rawValue)): \(command.executable.path) port=\(config.port)"
                + " url=\(config.serverURL.absoluteString)"
                + " recorder=\(config.recorder?.path ?? "(server default)") data_root=\(config.dataRoot ?? "(server default)")")

        let process = Process()
        process.executableURL = command.executable
        process.arguments = command.arguments
        process.currentDirectoryURL = command.currentDirectory
        process.environment = command.environment
        process.standardInput = FileHandle.nullDevice
        process.standardOutput = log
        process.standardError = log
        generation += 1
        let generation = self.generation
        process.terminationHandler = { [weak self] exited in
            let code = exited.terminationStatus
            let signaled = exited.terminationReason == .uncaughtSignal
            Task { @MainActor in
                self?.processDidExit(generation: generation, code: code, signaled: signaled)
            }
        }
        do {
            if mode == .bundled {
                guard let runtimeSyncOwnership else {
                    throw StartupFailure(message: "同梱サーバーの所有管理がありません")
                }
                // Keep durable ownership after readiness as well. If the app dies before
                // HTTP bind, another launch must not roll back this live server's venv.
                try runtimeSyncOwnership.start(process)
            } else {
                try process.run()
            }
        } catch {
            note("could not launch: \(error.localizedDescription)")
            if process.isRunning {
                self.process = process
                processGroup = getpgid(process.processIdentifier)
                stopRequested = false
            } else {
                closeLog()
            }
            throw StartupFailure(message: "起動できません: \(error.localizedDescription)")
        }
        self.process = process
        processGroup = getpgid(process.processIdentifier)
        stopRequested = false
        state = .starting(since: Date())
        let pid = process.processIdentifier
        note("spawned pid \(pid) (process group \(processGroup)); waiting up to \(Int(Self.startupTimeout)) s for get_server_info")

        let deadline = Date().addingTimeInterval(Self.startupTimeout)
        while Date() < deadline {
            try await Task.sleep(for: Self.startupPollInterval)
            guard self.process === process, process.isRunning else {
                throw StartupFailure(message: "サーバーが起動確認前に終了しました（ログ参照）")
            }
            if let info = await serverInfo() {
                try Task.checkCancellation()
                guard self.process === process, process.isRunning else {
                    throw StartupFailure(message: "サーバーが起動確認中に終了しました（ログ参照）")
                }
                try identity?.validate(info)
                return pid
            }
        }
        throw StartupFailure(message: ServerState.startupTimeoutMessage)
    }

    private func serverInfo() async -> ServerInfo? {
        do {
            let result = try await client.callTool(ToolCatalog.getServerInfo, arguments: [:])
            guard let content = result.structuredContent else { return nil }
            return try JSONDecoder().decode(ServerInfo.self, from: content.serialized())
        } catch {
            await client.reset()
            return nil
        }
    }

    private func recoverFailedInstallation() -> String? {
        guard let runtimeInstallation else { return nil }
        guard process == nil, syncProcess?.isRunning != true else {
            return "所有プロセスの停止を確認できないため、旧環境の復旧を保留しました"
        }
        do {
            // A process that appeared during a long sync also must not have its files
            // replaced under it. Leave the journal for a safe retry after it is stopped.
            try LocalServerPort.requireAvailable(config.port)
            try runtimeSyncOwnership?.requireIdle()
            let result = try runtimeInstallation.recover()
            if result == .rolledBack { note("restored previous runtime; startup remains failed") }
            return nil
        } catch {
            return "旧環境の復旧を完了できません: \(error.localizedDescription)"
        }
    }

    // MARK: Bundled runtime sync

    private struct RuntimeSyncFailure: LocalizedError {
        let step: String
        let reason: String
        var errorDescription: String? { "環境の準備に失敗（\(step)）: \(reason)" }
    }

    private struct SyncCommandFailure: LocalizedError {
        let command: String
        let status: Int32
        let signaled: Bool
        var errorDescription: String? {
            "\(command) が \(signaled ? "シグナル" : "exit") \(status) で終了（runtime.log 参照）"
        }
    }

    /// Compare the bundle's `runtime/manifest.json` with `<data root>/runtime/installed.json`
    /// and, when they differ (or installed.json is missing / unreadable), run the
    /// `RuntimeSyncPlan`: uv output goes to runtime.log, the menu shows each step as
    /// 「環境を準備中…（<step>）」. Throws `CancellationError` when `stop()` cancels the start,
    /// `RuntimeSyncFailure` otherwise — the caller surfaces it as `.failed` (retry via
    /// 「サーバーを再起動」), never by falling back to repo mode.
    private func syncBundledRuntimeIfNeeded(_ runtime: BundledRuntime) async throws -> BundledServerIdentity {
        let manifest: RuntimeManifest
        let manifestData: Data
        guard let runtimeInstallation else {
            throw RuntimeSyncFailure(step: "復旧", reason: "ランタイムの排他制御がありません")
        }
        let recovery = try runtimeInstallation.recover()
        if recovery == .rolledBack { note("recovered an interrupted runtime installation") }
        do {
            manifestData = try Data(contentsOf: runtime.manifest)
            manifest = try JSONDecoder().decode(RuntimeManifest.self, from: manifestData)
        } catch {
            throw RuntimeSyncFailure(step: "manifest 読み込み", reason: error.localizedDescription)
        }
        let identity = try BundledServerIdentity.load(config: config, manifest: manifest)
        let paths = config.runtimePaths
        // installed.json is the marker, but a venv deleted from under it must also re-sync —
        // otherwise every launch would fail with no path back to a working state.
        let venvIntact = FileManager.default.isExecutableFile(atPath: paths.serverExecutable.path)
            && FileManager.default.isExecutableFile(atPath: paths.venv.appendingPathComponent("bin/python3").path)
        guard manifest.needsSync(installed: RuntimeManifest.loadInstalled(from: paths.installedManifest))
            || !venvIntact
        else {
            return identity
        }
        let log: FileHandle
        do {
            log = try openLog(at: config.runtimeLogFile)
        } catch {
            throw RuntimeSyncFailure(step: "runtime.log を開く", reason: error.localizedDescription)
        }
        defer { try? log.close() }
        logLine(
            log,
            "runtime sync: app_version=\(manifest.appVersion) python=\(manifest.python)"
                + " uv=\(manifest.uvVersion) → \(paths.root.path)")
        let plan = RuntimeSyncPlan(bundle: runtime, paths: paths, manifest: manifest)
        for step in plan.steps {
            try Task.checkCancellation()
            state = .preparing(step: step.name)
            logLine(log, "step: \(step.name)")
            do {
                switch step {
                case .run(_, let command):
                    try await runSyncCommand(command, log: log)
                case .activate:
                    try runtimeInstallation.activate(manifest: manifestData)
                }
            } catch is CancellationError {
                logLine(log, "cancelled during: \(step.name)")
                throw CancellationError()
            } catch {
                logLine(log, "failed: \(step.name): \(error.localizedDescription)")
                throw RuntimeSyncFailure(step: step.name, reason: error.localizedDescription)
            }
        }
        logLine(log, "runtime candidate activated; retaining previous venv and marker until server verification")
        return identity
    }

    /// Run one sync subprocess (uv) with stdout/stderr appended to runtime.log. No hard
    /// timeout: the first sync downloads Python + several hundred MB of wheels. Cancellation
    /// (quit / stop) gives this owned uv process a short grace period before SIGKILL.
    private func runSyncCommand(_ command: RuntimeSyncPlan.Command, log: FileHandle) async throws {
        guard let runtimeSyncOwnership else {
            throw RuntimeSyncFailure(step: "起動", reason: "準備プロセスの所有管理がありません")
        }
        let process = Process()
        process.executableURL = command.executable
        process.arguments = command.arguments
        var environment = ProcessInfo.processInfo.environment
        for (key, value) in command.environment {
            environment[key] = value
        }
        process.environment = environment
        process.standardInput = FileHandle.nullDevice
        process.standardOutput = log
        process.standardError = log
        syncProcess = process
        defer {
            // Retain ownership and the runtime lease if even SIGKILL did not finish it.
            if !process.isRunning { syncProcess = nil }
        }
        try runtimeSyncOwnership.start(process)
        while process.isRunning {
            if Task.isCancelled {
                guard await stopSyncProcess() else {
                    throw RuntimeSyncFailure(step: "キャンセル", reason: "環境準備プロセスを停止できません")
                }
                break
            }
            try? await Task.sleep(for: .milliseconds(200))
        }
        process.waitUntilExit()  // already gone: reaps immediately
        try runtimeSyncOwnership.finish()
        if Task.isCancelled {
            throw CancellationError()
        }
        let signaled = process.terminationReason == .uncaughtSignal
        if signaled || process.terminationStatus != 0 {
            throw SyncCommandFailure(
                command: ([command.executable.lastPathComponent] + command.arguments.prefix(2))
                    .joined(separator: " "),
                status: process.terminationStatus, signaled: signaled)
        }
    }

    private func stopSyncProcess() async -> Bool {
        guard let process = syncProcess else { return true }
        let pid = process.processIdentifier
        let group = getpgid(pid)
        if process.isRunning { process.terminate() }
        if !(await waitForExit(of: process, timeout: Self.killGrace)) {
            killTree(pid: pid, group: group)
            guard await waitForExit(of: process, timeout: Self.killGrace) else { return false }
        }
        process.waitUntilExit()
        do {
            try runtimeSyncOwnership?.finish()
        } catch {
            note("runtime sync ownership retained: \(error.localizedDescription)")
            return false
        }
        syncProcess = nil
        return true
    }

    // MARK: Exit

    private func processDidExit(generation: Int, code: Int32, signaled: Bool) {
        guard generation == self.generation else {
            return  // an older process; its state was already handled
        }
        finishProcess(exitCode: code, signaled: signaled)
    }

    private func finishProcess(exitCode: Int32, signaled: Bool) {
        guard process != nil else {
            return
        }
        note("narumi-server exited: \(signaled ? "signal" : "exit") \(exitCode)")
        process = nil
        processGroup = 0
        closeLog()
        // Keep recovery context until stop()/the failed-start handler has restored any
        // pending installation. A delayed exit after a previous timeout must not discard it.
        state = state.afterExit(code: exitCode, signaled: signaled, requested: stopRequested)
    }

    private func waitForExit(of process: Process, timeout: TimeInterval) async -> Bool {
        let deadline = Date().addingTimeInterval(timeout)
        while process.isRunning {
            if Date() >= deadline {
                return false
            }
            // The caller is often an already-cancelled startup task. Sleeping directly in
            // it would throw immediately and spin until the process exits.
            await Self.shutdownPause()
        }
        return true
    }

    /// SIGKILL the child's process group when it has one of its own (Foundation.Process spawns
    /// children as group leaders, so uv, python and the recorder all go); else just the pid.
    private func killTree(pid: pid_t, group: pid_t) {
        if group > 0, group != getpgrp() {
            killpg(group, SIGKILL)
        } else {
            kill(pid, SIGKILL)
        }
    }

    private nonisolated static func shutdownPause() async {
        await withCheckedContinuation { continuation in
            DispatchQueue.global().asyncAfter(deadline: .now() + .milliseconds(100)) {
                continuation.resume()
            }
        }
    }

    // MARK: Log

    private func openLog(at url: URL) throws -> FileHandle {
        try Self.prepareLogFile(at: url)
        let handle = try FileHandle(forWritingTo: url)
        let attributes = try FileManager.default.attributesOfItem(atPath: url.path)
        let size = (attributes[.size] as? NSNumber)?.uint64Value ?? 0
        if size > Self.logRotateBytes {
            try handle.truncate(atOffset: 0)
        } else {
            try handle.seekToEnd()
        }
        return handle
    }

    private func closeLog() {
        try? logHandle?.close()
        logHandle = nil
    }

    /// App-side line in the server log (same file, so 「ログを開く」 shows both sides), mirrored
    /// to stderr for runs from a terminal.
    private func note(_ message: String) {
        let line = "\(ISO8601DateFormatter().string(from: Date())) narumi.app: \(message)\n"
        Self.stderr(line)
        guard let logHandle else {
            return
        }
        _ = try? logHandle.seekToEnd()
        try? logHandle.write(contentsOf: Data(line.utf8))
    }

    /// Same format into an explicit handle (the runtime.log during a sync).
    private func logLine(_ handle: FileHandle, _ message: String) {
        let line = "\(ISO8601DateFormatter().string(from: Date())) narumi.app: \(message)\n"
        Self.stderr(line)
        _ = try? handle.seekToEnd()
        try? handle.write(contentsOf: Data(line.utf8))
    }

    nonisolated static func stderr(_ line: String) {
        try? FileHandle.standardError.write(contentsOf: Data(line.utf8))
    }
}
