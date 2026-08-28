import Foundation

/// One desktop-owned permission workflow. Failed writes are never retried automatically;
/// a fresh snapshot from the originating server instance must prove setup is no longer busy.
public struct RecordingPermissionSetupState: Equatable, Sendable {
    public enum PermissionState: Equatable, Sendable {
        case granted, notGranted, unknown, helperUnavailable, unreachable
    }

    public struct PendingAction: Equatable, Sendable {
        public let permission: RecordingPermission
        public let action: RecordingPermissionAction
    }

    public struct ActionToken: Equatable, Sendable {
        fileprivate let generation: UInt64
        fileprivate let revision: UInt64
    }

    public struct SnapshotToken: Equatable, Sendable {
        /// Use this value for the actual request; an unknown/old contract forces false.
        public let refreshPermissions: Bool
        fileprivate let generation: UInt64
        fileprivate let revision: UInt64
    }

    /// Construct only from verified owned-process shutdown, never from launcher state,
    /// loss of connectivity, or `managesProcess == false`.
    public struct OwnedProcessTerminationEvidence: Equatable, Sendable {
        public let connectionGeneration: UInt64
        public let serverPID: Int32
        public let serverExited: Bool
        public let allOwnedChildrenExited: Bool

        public init(
            connectionGeneration: UInt64, serverPID: Int32,
            serverExited: Bool, allOwnedChildrenExited: Bool
        ) {
            self.connectionGeneration = connectionGeneration
            self.serverPID = serverPID
            self.serverExited = serverExited
            self.allOwnedChildrenExited = allOwnedChildrenExited
        }
    }

    private struct Origin: Equatable, Sendable {
        let generation: UInt64
        let serverPID: Int32?
        let serverInstanceID: String?
        var recoveryAllowed: Bool
    }

    public private(set) var serverState: ServerState = .notConfigured
    public private(set) var connectionGeneration: UInt64 = 0
    public private(set) var serverInfo: ServerInfo?
    public private(set) var permissions: RecorderPermissions?
    public private(set) var serverReachable = false
    public private(set) var serverSetupInProgress = false
    public private(set) var pendingAction: PendingAction?
    public private(set) var errorMessage: String?
    public private(set) var settingsOpened = false
    public private(set) var recordingBusy = false

    private var revision: UInt64 = 0
    private var actionToken: ActionToken?
    private var snapshotToken: SnapshotToken?
    private var origin: Origin?

    public init() {}

    public var supportsSetup: Bool {
        RecordingPermissionContract.supportsSetup(
            serverInfo?.contractVersion, serverInstanceID: serverInfo?.serverInstanceID)
    }
    public var helperAvailable: Bool? {
        guard let serverInfo else { return nil }
        return !(serverInfo.diagnostics.recorderPath ?? "").isEmpty
    }
    public var blocked: Bool { pendingAction != nil || serverSetupInProgress }
    public var isActionInFlight: Bool { actionToken != nil }
    public var isAwaitingReconciliation: Bool { blocked && !isActionInFlight }
    public var needsSetup: Bool {
        permissionState(.microphone) != .granted || permissionState(.screenRecording) != .granted
    }
    public var ready: Bool { !needsSetup && !blocked }
    public var recoveryConnectionGeneration: UInt64? {
        guard origin?.recoveryAllowed == true else { return nil }
        return origin?.generation
    }
    public var recoveryServerPID: Int32? {
        guard origin?.recoveryAllowed == true else { return nil }
        return origin?.serverPID
    }

    public func permissionState(_ permission: RecordingPermission) -> PermissionState {
        guard serverReachable else { return .unreachable }
        guard helperAvailable == true else { return .helperUnavailable }
        let value = permission == .microphone ? permissions?.microphone : permissions?.screenRecording
        switch value {
        case "granted": return .granted
        case "denied": return .notGranted
        default: return .unknown
        }
    }

    public func canRequest(_ permission: RecordingPermission) -> Bool {
        guard canAct else { return false }
        switch permissionState(permission) {
        case .unknown: return true
        case .notGranted: return permission == .screenRecording
        case .granted, .helperUnavailable, .unreachable: return false
        }
    }

    public func canOpenSettings(_ permission: RecordingPermission) -> Bool { canAct }

    public mutating func setRecordingBusy(_ busy: Bool) { recordingBusy = busy }

    /// Preserve unresolved work across reconnects, but revoke all old network tokens.
    public mutating func connectionChanged(to state: ServerState) {
        serverState = state
        connectionGeneration &+= 1
        invalidateSnapshot()
        actionToken = nil
        serverInfo = nil
        permissions = nil
        serverReachable = false
        switch state {
        case .failed, .stopped: break
        case .running(let pid):
            if origin?.serverPID != pid { origin?.recoveryAllowed = false }
        case .notConfigured, .preparing, .starting, .external:
            origin?.recoveryAllowed = false
        }
    }

    public mutating func beginSnapshot(refreshPermissions: Bool = false) -> SnapshotToken? {
        guard snapshotToken == nil else { return nil }
        revision &+= 1
        let token = SnapshotToken(
            refreshPermissions: refreshPermissions && supportsSetup,
            generation: connectionGeneration, revision: revision)
        snapshotToken = token
        return token
    }

    @discardableResult
    public mutating func finishSnapshot(_ token: SnapshotToken, info: ServerInfo) -> Bool {
        guard isCurrentSnapshot(token) else { return false }
        snapshotToken = nil
        // A typed action response may already be queued on the UI actor. It must not
        // overwrite a newer instance's snapshot, even after its HTTP request completed.
        if actionToken != nil && origin?.serverInstanceID != info.serverInstanceID {
            actionToken = nil
        }
        serverInfo = info
        permissions = info.capabilities.permissions
        serverReachable = true
        if info.capabilities.permissionSetupInProgress {
            if !blocked { origin = currentOrigin }
            serverSetupInProgress = true
        } else if token.refreshPermissions && supportsSetup && !isActionInFlight
            && (!blocked || origin?.serverInstanceID == info.serverInstanceID) {
            pendingAction = nil
            serverSetupInProgress = false
            origin = nil
            errorMessage = nil
        }
        return true
    }

    @discardableResult
    public mutating func failSnapshot(_ token: SnapshotToken) -> Bool {
        guard isCurrentSnapshot(token) else { return false }
        snapshotToken = nil
        serverReachable = false
        return true
    }

    public mutating func beginAction(
        permission: RecordingPermission, action: RecordingPermissionAction
    ) -> ActionToken? {
        let allowed = action == .request ? canRequest(permission) : canOpenSettings(permission)
        guard allowed else { return nil }
        invalidateSnapshot()
        let token = ActionToken(generation: connectionGeneration, revision: revision)
        pendingAction = PendingAction(permission: permission, action: action)
        actionToken = token
        origin = currentOrigin
        errorMessage = nil
        settingsOpened = false
        return token
    }

    public func isCurrentAction(_ token: ActionToken) -> Bool {
        actionToken == token && token.generation == connectionGeneration && pendingAction != nil
    }

    @discardableResult
    public mutating func finishAction(
        _ token: ActionToken, response: ConfigureRecordingPermissionResponse
    ) -> Bool {
        guard isCurrentAction(token) else { return false }
        guard response.permission == pendingAction?.permission, response.action == pendingAction?.action else {
            failAction(token, message: "権限操作の応答を確認できません。状態を再確認してください")
            return false
        }
        invalidateSnapshot()
        actionToken = nil
        pendingAction = nil
        permissions = response.permissions
        serverReachable = true
        settingsOpened = response.settingsOpened
        errorMessage = nil
        if !blocked { origin = nil }
        return true
    }

    /// Even an HTTP failure can leave the server's helper running. Only a later fresh
    /// snapshot, or verified shutdown of the originating owned process group, resolves it.
    @discardableResult
    public mutating func failAction(_ token: ActionToken, message: String? = nil) -> Bool {
        guard isCurrentAction(token) else { return false }
        invalidateSnapshot()
        actionToken = nil
        serverReachable = false
        errorMessage = message ?? "権限操作の結果を確認できません。自動再送せず、状態を再確認します"
        return true
    }

    @discardableResult
    public mutating func confirmOwnedProcessTermination(_ evidence: OwnedProcessTerminationEvidence) -> Bool {
        guard blocked, let origin, origin.recoveryAllowed,
            evidence.connectionGeneration == origin.generation,
            evidence.serverPID > 0, evidence.serverPID == origin.serverPID,
            evidence.serverExited, evidence.allOwnedChildrenExited else { return false }
        invalidateSnapshot()
        actionToken = nil
        pendingAction = nil
        serverSetupInProgress = false
        serverReachable = false
        serverInfo = nil
        permissions = nil
        errorMessage = nil
        self.origin = nil
        return true
    }

    private var canAct: Bool {
        supportsSetup && serverReachable && helperAvailable == true && !blocked && !recordingBusy
    }
    private var currentOrigin: Origin {
        if case .running(let pid) = serverState, pid > 0 {
            return Origin(
                generation: connectionGeneration, serverPID: pid,
                serverInstanceID: serverInfo?.serverInstanceID, recoveryAllowed: true)
        }
        return Origin(
            generation: connectionGeneration, serverPID: nil,
            serverInstanceID: serverInfo?.serverInstanceID, recoveryAllowed: false)
    }
    private func isCurrentSnapshot(_ token: SnapshotToken) -> Bool {
        snapshotToken == token && token.generation == connectionGeneration
    }
    private mutating func invalidateSnapshot() {
        revision &+= 1
        snapshotToken = nil
    }
}
