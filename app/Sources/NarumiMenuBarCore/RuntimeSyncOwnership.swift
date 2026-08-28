import Darwin
import Foundation

/// Durable ownership of uv steps and the bundled server. The app's flock alone is
/// insufficient: after an app crash a child may still be writing/using the runtime without
/// listening on HTTP (including a server that has not bound its port yet).
/// An orphan is never adopted or signalled here; live or uncertain ownership blocks writes.
public final class RuntimeSyncOwnership {
    struct Identity: Codable, Equatable {
        var pid: Int32
        var startedSeconds: UInt64
        var startedMicroseconds: UInt64
        var processGroup: Int32

        func isSameProcess(as other: Identity) -> Bool {
            pid == other.pid && startedSeconds == other.startedSeconds
                && startedMicroseconds == other.startedMicroseconds
        }
    }

    struct Record: Codable {
        var formatVersion = 1
        var token: String
        var bootSession: String
        var app: Identity
        var child: Identity?
    }

    struct Inspection {
        var bootSession: () throws -> String
        var identity: (Int32) throws -> Identity?
        var groupExists: (Int32) throws -> Bool
        static var system: Inspection {
            Inspection(bootSession: bootIdentifier, identity: processIdentity, groupExists: processGroupExists)
        }
    }

    struct Failure: LocalizedError {
        let message: String
        var errorDescription: String? { "ランタイムを開始できません: \(message)" }
    }

    enum Checkpoint { case intentWritten, childLaunched, childRecorded, childReleased }
    var checkpoint: ((Checkpoint) throws -> Void)?
    private let paths: RuntimePaths
    private let inspection: Inspection
    private var currentToken: String?
    var recordURL: URL { paths.root.appendingPathComponent("sync-owner.json") }

    // The shell cannot run uv until its process identity has been persisted. This avoids
    // losing the identity of very short-lived uv commands. The gate is private to this
    // launch, and stdin returns to /dev/null before exec; argv is never shell-interpolated.
    static let gateScript = """
        read -r narumi_runtime_gate || exit 125
        [ "$narumi_runtime_gate" = "$1" ] || exit 125
        shift
        exec "$@" < /dev/null
        """

    public convenience init(paths: RuntimePaths) {
        self.init(paths: paths, inspection: .system)
    }

    init(paths: RuntimePaths, inspection: Inspection) {
        self.paths = paths
        self.inspection = inspection
    }

    /// Call while holding RuntimeLease and before recovery or any uv command. Unknown
    /// pre-PID intents fail closed for the current boot. A new boot proves all old children
    /// are gone, including the intent/run boundary, and permits removal of the stale record.
    public func requireIdle() throws {
        guard FileManager.default.fileExists(atPath: recordURL.path) else { return }
        let record = try read()
        if record.bootSession == (try inspection.bootSession()) {
            guard let child = record.child else {
                throw Failure(message: "前回の準備プロセスの識別情報が未確定です。安全のため再同期を停止しました。Mac再起動後に再試行できます")
            }
            if let current = try inspection.identity(child.pid), child.isSameProcess(as: current) {
                throw Failure(message: "前回のランタイムプロセス（pid \(child.pid)）が動作中です。終了後に再試行してください（自動停止はしません）")
            }
            if try inspection.groupExists(child.processGroup) {
                throw Failure(message: "前回のランタイムの子プロセスが残っています。終了後に再試行してください（自動停止はしません）")
            }
        }
        try remove(token: record.token)
    }

    /// Configures and starts the supplied Process with a short stdin gate. The caller still
    /// owns it and handles graceful stopping; this helper never kills another app's child.
    public func start(_ process: Process) throws {
        try requireIdle()
        guard let executable = process.executableURL,
            let app = try inspection.identity(getpid())
        else {
            throw Failure(message: "起動元のプロセスを識別できません")
        }
        let token = UUID().uuidString
        var record = Record(token: token, bootSession: try inspection.bootSession(), app: app)
        try write(record)
        currentToken = token
        let gate = Pipe()
        defer {
            try? gate.fileHandleForWriting.close()
            try? gate.fileHandleForReading.close()
        }
        process.arguments = ["-c", Self.gateScript, "narumi-runtime-sync", token, executable.path]
            + (process.arguments ?? [])
        process.executableURL = URL(fileURLWithPath: "/bin/sh")
        process.standardInput = gate
        var identityRecorded = false
        do {
            try checkpoint?(.intentWritten)
            try process.run()
            try gate.fileHandleForReading.close()
            try checkpoint?(.childLaunched)
            guard let child = try inspection.identity(process.processIdentifier), child.processGroup > 0,
                child.processGroup != app.processGroup
            else {
                throw Failure(message: "準備プロセスの専用グループを識別できません")
            }
            record.child = child
            try write(record)
            identityRecorded = true
            try checkpoint?(.childRecorded)
            try gate.fileHandleForWriting.write(contentsOf: Data((token + "\n").utf8))
            try gate.fileHandleForWriting.close()
            try checkpoint?(.childReleased)
        } catch {
            // Until identityRecorded, no gate bytes were written, so the wrapper cannot
            // have started uv. A process crash does not run this cleanup and leaves the
            // durable intent for the next launch to reject conservatively.
            if !identityRecorded {
                try? remove(token: token)
                currentToken = nil
            }
            throw error
        }
    }

    /// A uv exit alone is insufficient: builds may leave a subprocess in its process group.
    /// Keep the durable record until both the original process and its group are gone.
    public func finish() throws {
        guard let currentToken else { return }
        guard try read().token == currentToken else {
            throw Failure(message: "準備プロセスの所有記録が変更されています")
        }
        try requireIdle()
        self.currentToken = nil
    }

    private func read() throws -> Record {
        let record = try JSONDecoder().decode(Record.self, from: Data(contentsOf: recordURL))
        guard record.formatVersion == 1, !record.token.isEmpty, !record.bootSession.isEmpty,
            record.app.pid > 0,
            record.child.map({ $0.pid > 0 && $0.processGroup > 0 && $0.startedSeconds > 0 }) ?? true
        else { throw Failure(message: "所有記録が不正です。自動削除は行いません") }
        return record
    }

    private func write(_ record: Record) throws {
        try JSONEncoder().encode(record).write(to: recordURL, options: .atomic)
    }

    private func remove(token: String) throws {
        guard try read().token == token else {
            throw Failure(message: "準備プロセスの所有記録が変更されています")
        }
        try FileManager.default.removeItem(at: recordURL)
    }

    static func processIdentity(_ pid: Int32) throws -> Identity? {
        var info = proc_bsdinfo()
        let size = Int32(MemoryLayout<proc_bsdinfo>.size)
        if proc_pidinfo(pid, PROC_PIDTBSDINFO, 0, &info, size) == size {
            return Identity(
                pid: pid, startedSeconds: info.pbi_start_tvsec, startedMicroseconds: info.pbi_start_tvusec,
                processGroup: Int32(info.pbi_pgid))
        }
        if kill(pid, 0) != 0, errno == ESRCH { return nil }
        throw Failure(message: "pid \(pid) の開始時刻を確認できません")
    }

    static func processGroupExists(_ group: Int32) throws -> Bool {
        guard group > 0 else { throw Failure(message: "プロセスグループが不正です") }
        if killpg(group, 0) == 0 { return true }
        if errno == ESRCH { return false }
        throw Failure(message: "プロセスグループ \(group) の終了を確認できません")
    }

    static func bootIdentifier() throws -> String {
        var length = 0
        guard sysctlbyname("kern.bootsessionuuid", nil, &length, nil, 0) == 0, length > 1, length < 256 else {
            throw Failure(message: "Macの起動セッションを確認できません")
        }
        var bytes = [UInt8](repeating: 0, count: length)
        let result = bytes.withUnsafeMutableBytes { sysctlbyname("kern.bootsessionuuid", $0.baseAddress, &length, nil, 0) }
        guard result == 0 else { throw Failure(message: "Macの起動セッションを確認できません") }
        return String(decoding: bytes.prefix { $0 != 0 }, as: UTF8.self)
    }
}
