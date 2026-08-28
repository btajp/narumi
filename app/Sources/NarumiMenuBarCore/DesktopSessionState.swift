import Foundation

/// The menu and main window render the same recording state. Network failures keep the
/// last known recording visible; only a current server response may confirm it stopped.
public struct DesktopSessionState: Equatable, Sendable {
    public enum Operation: Equatable, Sendable {
        case starting
        case stopping
    }

    public struct Token: Equatable, Sendable {
        fileprivate var generation: UInt64
        fileprivate var revision: UInt64
    }

    public private(set) var serverState: ServerState = .notConfigured
    public private(set) var connectionGeneration: UInt64 = 0
    public private(set) var serverReachable = false
    public private(set) var recordingCapable: Bool?
    public private(set) var recording = RecordingStatus(active: false)
    public private(set) var recordingIsConfirmed = false
    public private(set) var operation: Operation?
    public private(set) var terminating = false
    public private(set) var installingUpdate = false
    public private(set) var hasPendingJobRequests = false
    public private(set) var hasPendingStopRequest = false
    public private(set) var permissionSetupBlocked = false
    public private(set) var needsPermissionSetup = false

    private var revision: UInt64 = 0
    private var pendingPoll: Token?
    private var pendingOperation: Token?

    public init() {}

    public var canStart: Bool {
        serverState.pollsServerInfo && serverReachable && recordingCapable == true
            && recordingIsConfirmed && !recording.active && operation == nil
            && !terminating && !installingUpdate && !hasPendingJobRequests
            && !permissionSetupBlocked && !needsPermissionSetup
    }

    public var canStop: Bool {
        serverState.pollsServerInfo && serverReachable && recording.active
            && operation == nil && !terminating && !installingUpdate && !hasPendingStopRequest
    }

    public var menuSymbolName: String { recording.active ? "record.circle.fill" : "waveform" }

    public var accessibilityLabel: String { "narumi: \(statusText)" }

    public var statusText: String {
        if terminating { return "終了処理中…" }
        if installingUpdate { return "アップデートを適用中…" }
        if operation == .starting { return "録画を開始しています…" }
        if operation == .stopping { return "録画を停止・保存しています…" }
        if recording.active {
            return recordingIsConfirmed ? "録画中" : "録画中（接続・状態を再確認中）"
        }
        if permissionSetupBlocked { return "録画の権限設定が完了するまで待機しています" }
        if hasPendingJobRequests { return "操作結果を確認中です。確認まで更新・新規録画を待機します" }
        switch serverState {
        case .preparing(let step): return "環境を準備中…（\(step)）"
        case .starting: return "録画の準備中…"
        case .notConfigured: return "録画環境を確認しています…"
        case .failed: return "起動できませんでした。診断とログを確認してください"
        case .stopped: return "サーバーが停止しています"
        case .running, .external:
            if !serverReachable || !recordingIsConfirmed { return "接続・録画状態を確認中…" }
            if needsPermissionSetup { return "録画の権限を設定してください。診断から確認できます" }
            if recordingCapable != true { return "録画の準備が必要です。診断で権限・環境を確認してください" }
            return "録画を開始できます"
        }
    }

    /// Called on every launcher transition, including a new connection to the same URL.
    /// In-flight results belong to the previous connection and cannot clear an active mark.
    public mutating func connectionChanged(to state: ServerState) {
        serverState = state
        connectionGeneration &+= 1
        invalidateRequests()
        operation = nil
        pendingOperation = nil
        serverReachable = false
        recordingCapable = nil
        recordingIsConfirmed = false
    }

    public mutating func beginPoll() -> Token? {
        guard serverState.pollsServerInfo, operation == nil, !terminating, pendingPoll == nil else {
            return nil
        }
        revision &+= 1
        let token = currentToken
        pendingPoll = token
        return token
    }

    public func isCurrentPoll(_ token: Token) -> Bool {
        pendingPoll == token && token == currentToken && operation == nil && !terminating
    }

    @discardableResult
    public mutating func finishPoll(_ token: Token, info: ServerInfoSummary, recording: RecordingStatus) -> Bool {
        guard isCurrentPoll(token) else { return false }
        pendingPoll = nil
        serverReachable = true
        recordingCapable = info.recordingCapable
        self.recording = recording
        recordingIsConfirmed = true
        return true
    }

    @discardableResult
    public mutating func failPoll(_ token: Token) -> Bool {
        guard isCurrentPoll(token) else { return false }
        pendingPoll = nil
        serverReachable = false
        recordingIsConfirmed = false
        return true
    }

    public mutating func beginStart() -> Token? {
        guard canStart else { return nil }
        return beginOperation(.starting)
    }

    public mutating func beginStop() -> Token? {
        guard canStop else { return nil }
        return beginOperation(.stopping)
    }

    public func isCurrentOperation(_ token: Token) -> Bool {
        pendingOperation == token && token == currentToken && operation != nil
            && !terminating && !installingUpdate
    }

    /// The start dialog was cancelled before a request was sent, so the known status is valid.
    @discardableResult
    public mutating func cancelStart(_ token: Token) -> Bool {
        guard isCurrentOperation(token, .starting) else { return false }
        endOperation()
        return true
    }

    @discardableResult
    public mutating func finishStart(_ token: Token, recording: RecordingStatus) -> Bool {
        guard isCurrentOperation(token, .starting), recording.active else { return false }
        self.recording = recording
        recordingIsConfirmed = true
        endOperation()
        return true
    }

    @discardableResult
    public mutating func finishStop(_ token: Token) -> Bool {
        guard isCurrentOperation(token, .stopping) else { return false }
        recording = RecordingStatus(active: false)
        recordingIsConfirmed = true
        endOperation()
        return true
    }

    /// A failed request can have reached the server. Keep the visible recording and require
    /// a fresh poll before permitting a new start or an update.
    @discardableResult
    public mutating func failOperation(_ token: Token) -> Bool {
        guard pendingOperation == token, token == currentToken else { return false }
        recordingIsConfirmed = false
        endOperation()
        return true
    }

    public mutating func setInstallingUpdate(_ installing: Bool) {
        installingUpdate = installing
    }

    public mutating func setJobRequestState(pending: Bool, pendingStop: Bool) {
        hasPendingJobRequests = pending
        hasPendingStopRequest = pendingStop
    }

    public mutating func setPermissionSetupState(blocked: Bool, needsSetup: Bool) {
        permissionSetupBlocked = blocked
        needsPermissionSetup = needsSetup
    }

    public mutating func beginTermination() {
        terminating = true
        invalidateRequests()
    }

    /// Used only after an explicit quit/restart stop has succeeded, outside the normal UI
    /// operation. Update-triggered quits must never take this path with an active recording.
    public mutating func confirmStoppedForShutdown() {
        recording = RecordingStatus(active: false)
        recordingIsConfirmed = true
        invalidateRequests()
    }

    public func updateBlockReason(launcherBusy: Bool, knownJobsBusy: Bool) -> String? {
        if terminating { return "終了処理中のため更新を延期します" }
        if operation != nil { return "録画の開始・停止操作中のため更新を延期します" }
        if recording.active { return "録画中のため更新を延期します" }
        if permissionSetupBlocked { return "録画の権限設定が完了するまで更新を延期します" }
        if hasPendingJobRequests { return "操作結果を確認できるまで更新を延期します" }
        if launcherBusy { return "環境を準備中のため更新を延期します" }
        switch serverState {
        case .preparing, .starting: return "環境を準備中のため更新を延期します"
        default: break
        }
        if !serverReachable || !recordingIsConfirmed {
            return "録画状態を確認できるまで更新を延期します"
        }
        if knownJobsBusy { return "実行中のジョブがあるため更新を延期します" }
        return nil
    }

    public func shouldDeferUpdateTermination(
        updateOwnsTermination: Bool, updateInstalling: Bool, userRequestedQuit: Bool,
        launcherBusy: Bool, knownJobsBusy: Bool
    ) -> Bool {
        guard updateOwnsTermination, !userRequestedQuit else { return false }
        return !updateInstalling || updateBlockReason(launcherBusy: launcherBusy, knownJobsBusy: knownJobsBusy) != nil
    }

    private var currentToken: Token {
        Token(generation: connectionGeneration, revision: revision)
    }

    private mutating func invalidateRequests() {
        revision &+= 1
        pendingPoll = nil
    }

    private mutating func beginOperation(_ next: Operation) -> Token {
        invalidateRequests()
        operation = next
        let token = currentToken
        pendingOperation = token
        return token
    }

    private func isCurrentOperation(_ token: Token, _ expected: Operation) -> Bool {
        isCurrentOperation(token) && operation == expected
    }

    private mutating func endOperation() {
        operation = nil
        pendingOperation = nil
        invalidateRequests()
    }
}
