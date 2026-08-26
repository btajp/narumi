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
    var managesProcess: Bool { process?.isRunning ?? false }
    /// A start or stop is in flight (menu items that would race it are disabled meanwhile).
    var isBusy: Bool { startTask != nil || stopping }

    private let client: MCPClient
    private var process: Process?
    private var processGroup: pid_t = 0
    private var logHandle: FileHandle?
    private var startTask: Task<Void, Never>?
    private var stopping = false
    private var stopRequested = false
    private var generation = 0

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
    }

    /// SIGTERM, wait up to `timeout` for the graceful shutdown (recording finalization), then
    /// SIGKILL the whole process group (uv → python → recorder). Never leaves an orphan.
    func stop(timeout: TimeInterval = ServerLauncher.stopTimeout) async {
        if let startTask {
            startTask.cancel()
            await startTask.value
            self.startTask = nil
        }
        guard let process else {
            return
        }
        stopping = true
        defer { stopping = false }
        stopRequested = true
        let pid = process.processIdentifier
        note("stopping narumi-server (pid \(pid)): SIGTERM, waiting up to \(Int(timeout)) s")
        if process.isRunning {
            process.terminate()
        }
        var exited = await waitForExit(of: process, timeout: timeout)
        if !exited {
            note("no exit after \(Int(timeout)) s; SIGKILL to pid \(pid) / process group \(processGroup)")
            killTree(pid: pid)
            exited = await waitForExit(of: process, timeout: Self.killGrace)
        }
        if exited {
            process.waitUntilExit()  // already gone: reaps immediately
        } else {
            note("pid \(pid) is still alive after SIGKILL")
        }
        // Belt and braces: nothing of the tree may survive even if uv died before the server.
        if processGroup > 0, processGroup != getpgrp(), killpg(processGroup, 0) == 0 {
            note("process group \(processGroup) still has members; SIGKILL")
            killpg(processGroup, SIGKILL)
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
        if let process, process.isRunning {
            return
        }
        if await serverAnswers() {
            state = .external(config.serverURL)
            return
        }
        if Task.isCancelled {
            return
        }
        guard let repository = config.repository else {
            state = .notConfigured
            return
        }
        guard ServerConfig.isRepository(repository) else {
            state = .failed("narumi リポジトリではありません: \(repository.path)")
            return
        }
        guard let command = ServerCommand(config: config) else {
            state = .notConfigured
            return
        }
        let log: FileHandle
        do {
            log = try openLog()
        } catch {
            state = .failed("ログファイルを開けません: \(error.localizedDescription)")
            return
        }
        logHandle = log
        note(
            "starting narumi-server: repo=\(repository.path) port=\(config.port) url=\(config.serverURL.absoluteString)"
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
            try process.run()
        } catch {
            note("could not launch: \(error.localizedDescription)")
            closeLog()
            state = .failed("起動できません: \(error.localizedDescription)")
            return
        }
        self.process = process
        processGroup = getpgid(process.processIdentifier)
        stopRequested = false
        state = .starting(since: Date())
        let pid = process.processIdentifier
        note("spawned pid \(pid) (process group \(processGroup)); waiting up to \(Int(Self.startupTimeout)) s for get_server_info")

        let deadline = Date().addingTimeInterval(Self.startupTimeout)
        while Date() < deadline {
            do {
                try await Task.sleep(for: Self.startupPollInterval)
            } catch {
                return  // cancelled by stop(); it takes over the process
            }
            guard self.process === process, case .starting = state else {
                return  // exited meanwhile (handler updated the state) or replaced
            }
            if await serverAnswers() {
                state = .running(pid: pid)
                note("server answers at \(config.serverURL.absoluteString) (pid \(pid))")
                return
            }
        }
        guard self.process === process, case .starting = state else {
            return
        }
        let alive = process.isRunning
        state = state.afterStartupTimeout(processAlive: alive, exitCode: alive ? 0 : process.terminationStatus)
        note(alive ? "startup timeout: no answer after \(Int(Self.startupTimeout)) s; process left running" : "startup timeout: process already exited")
    }

    private func serverAnswers() async -> Bool {
        do {
            _ = try await client.callTool("get_server_info", arguments: [:])
            return true
        } catch {
            await client.reset()
            return false
        }
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
        state = state.afterExit(code: exitCode, signaled: signaled, requested: stopRequested)
    }

    private func waitForExit(of process: Process, timeout: TimeInterval) async -> Bool {
        let deadline = Date().addingTimeInterval(timeout)
        while process.isRunning {
            if Date() >= deadline {
                return false
            }
            try? await Task.sleep(for: .milliseconds(100))
        }
        return true
    }

    /// SIGKILL the child's process group when it has one of its own (Foundation.Process spawns
    /// children as group leaders, so uv, python and the recorder all go); else just the pid.
    private func killTree(pid: pid_t) {
        if processGroup > 0, processGroup != getpgrp() {
            killpg(processGroup, SIGKILL)
        } else {
            kill(pid, SIGKILL)
        }
    }

    // MARK: Log

    private func openLog() throws -> FileHandle {
        let url = config.logFile
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

    nonisolated static func stderr(_ line: String) {
        try? FileHandle.standardError.write(contentsOf: Data(line.utf8))
    }
}
