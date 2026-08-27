import Foundation

/// Lifecycle of `narumi-server` as seen by narumi.app.
public enum ServerState: Equatable, Sendable {
    /// No repository could be resolved; nothing was launched.
    case notConfigured
    /// A server already answered at the URL before we launched one (e.g. `scripts/dev.sh`);
    /// it is not ours to stop.
    case external(URL)
    /// Bundled runtime: the venv is being (re)built before launch (`RuntimeSyncPlan`);
    /// `step` names the current stage (Python 取得 / 依存インストール …).
    case preparing(step: String)
    /// Our process was spawned; waiting for `get_server_info` to answer.
    case starting(since: Date)
    /// Our process answers `get_server_info`.
    case running(pid: Int32)
    /// Our process exited: after `stop()`, or on its own once it had been running.
    case stopped(exitCode: Int32)
    /// Could not launch, startup timed out, or the process died unexpectedly. The message is
    /// shown as a tooltip and written to the log; the menu points at the log.
    case failed(String)

    public static let startupTimeoutMessage = "起動タイムアウト（ログ参照）"

    /// 「サーバーを再起動」 makes sense: everything but an external server / no repository.
    public var canRestart: Bool {
        switch self {
        case .external, .notConfigured: return false
        case .preparing, .starting, .running, .stopped, .failed: return true
        }
    }

    /// `get_server_info` is worth polling for the status line.
    public var pollsServerInfo: Bool {
        switch self {
        case .running, .external: return true
        case .notConfigured, .preparing, .starting, .stopped, .failed: return false
        }
    }

    /// State after the managed process exited.
    ///
    /// - `requested`: `stop()` asked for it (SIGTERM / SIGKILL) → always `.stopped`.
    /// - `signaled`: died from an uncaught signal → `.failed`.
    /// - Otherwise a non-zero exit while still starting is a launch failure (`uv` could not
    ///   run, port taken, …); once it was running a plain exit is `.stopped`.
    public func afterExit(code: Int32, signaled: Bool, requested: Bool) -> ServerState {
        if requested {
            return .stopped(exitCode: code)
        }
        if signaled {
            return .failed("シグナル \(code) で終了")
        }
        if case .starting = self, code != 0 {
            return .failed("起動時に終了 (exit \(code))")
        }
        return .stopped(exitCode: code)
    }

    /// State when `get_server_info` did not answer within the startup timeout. The process is
    /// left running when still alive (its log tells why); a dead one is simply stopped.
    public func afterStartupTimeout(processAlive: Bool, exitCode: Int32) -> ServerState {
        guard case .starting = self else {
            return self
        }
        return processAlive ? .failed(Self.startupTimeoutMessage) : .stopped(exitCode: exitCode)
    }

    /// Tooltip / log detail for the state.
    public var detail: String? {
        switch self {
        case .notConfigured:
            return "環境変数 NARUMI_REPO か「リポジトリを選択…」で narumi リポジトリを指定してください"
        case .external(let url):
            return "\(url.absoluteString)（このアプリは起動・停止しません）"
        case .preparing:
            return "初回は Python と依存のダウンロードが発生します（ログ: runtime.log）"
        case .starting(let since):
            return "開始 \(ISO8601DateFormatter().string(from: since))"
        case .running(let pid):
            return "pid \(pid)"
        case .stopped(let code):
            return "exit \(code)"
        case .failed(let message):
            return message
        }
    }
}

/// What the status line shows from the last `get_server_info` answer.
public struct ServerInfoSummary: Equatable, Sendable {
    public var version: String?
    public var contractVersion: String?
    public var recordingCapable: Bool?

    public init(version: String? = nil, contractVersion: String? = nil, recordingCapable: Bool? = nil) {
        self.version = version
        self.contractVersion = contractVersion
        self.recordingCapable = recordingCapable
    }
}

public enum ServerStatusText {
    /// Title of the 「サーバー: …」 menu item. `info` / `reachable` describe the last
    /// `get_server_info` poll and only matter for `.running` / `.external`.
    public static func title(for state: ServerState, info: ServerInfoSummary?, reachable: Bool) -> String {
        let body: String
        switch state {
        case .notConfigured:
            body = "未設定（リポジトリを選択してください）"
        case .external:
            body = reachable ? join("外部サーバーに接続", info) : "外部サーバーに接続できません"
        case .preparing(let step):
            body = "環境を準備中…（\(step)）"
        case .starting:
            body = "起動中…"
        case .running(let pid):
            body = reachable ? join("稼働中", info) : "応答なし (pid \(pid))"
        case .stopped(let code):
            body = "停止 (exit \(code))"
        case .failed:
            body = "起動失敗（ログ参照）"
        }
        return "サーバー: " + body
    }

    static func join(_ head: String, _ info: ServerInfoSummary?) -> String {
        var parts = [head]
        if let version = info?.version {
            parts.append("v\(version)")
        }
        if let contract = info?.contractVersion {
            parts.append("契約 \(contract)")
        }
        // capabilities.recording = "recording is possible on this machine" (recorder found +
        // permissions), not "a recording is running".
        if info?.recordingCapable == false {
            parts.append("録画不可")
        }
        return parts.joined(separator: " ")
    }
}
