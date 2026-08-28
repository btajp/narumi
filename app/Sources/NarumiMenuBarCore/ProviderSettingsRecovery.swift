import Foundation

/// A missing response is not evidence that a side effect did not happen. Recovery performs
/// status lookups only, retaining the original request and server identity where known.
public struct ProviderSettingsRecovery: Equatable, Sendable {
    public struct Authentication: Equatable, Sendable {
        public let connectionID: String
        public let startRequestID: String
        public fileprivate(set) var operationID: String?
        public fileprivate(set) var serverInstanceID: String?
        public fileprivate(set) var connectionRevision: Int?
        public fileprivate(set) var action: ProviderAuthAction?
        public fileprivate(set) var state: ProviderAuthOperationState
        public fileprivate(set) var authorizationURL: ProviderAuthorizationURL?
        public fileprivate(set) var userCode: ProviderUserCode?
        public fileprivate(set) var reasonMessage: String?

        public var unresolved: Bool { state == .pending || state == .unknown }
    }

    public struct Setup: Equatable, Sendable {
        public let providerID: ProviderID
        public let startRequestID: String
        public let resourceID: String
        public fileprivate(set) var jobID: String?
        public fileprivate(set) var state: ProviderSetupState

        public var unresolved: Bool { state == .queued || state == .running || state == .unknown }
    }

    public private(set) var authentications: [String: Authentication] = [:]
    public private(set) var setups: [ProviderID: Setup] = [:]

    public init() {}

    public var needsPolling: Bool {
        authentications.values.contains { $0.state == .pending }
            || setups.values.contains { $0.state == .queued || $0.state == .running }
    }

    public mutating func beginAuthentication(
        connectionID: String, requestID: String, connectionRevision: Int? = nil,
        action: ProviderAuthAction? = nil
    ) -> Bool {
        guard authentications[connectionID]?.unresolved != true else { return false }
        authentications[connectionID] = Authentication(
            connectionID: connectionID, startRequestID: requestID,
            connectionRevision: connectionRevision, action: action, state: .pending)
        return true
    }

    @discardableResult
    public mutating func receive(_ operation: ProviderAuthOperation) -> Bool {
        guard var pending = authentications[operation.connectionID],
            pending.startRequestID == operation.startRequestID else { return false }
        guard (pending.serverInstanceID == nil || pending.serverInstanceID == operation.serverInstanceID),
            (pending.operationID == nil || pending.operationID == operation.operationID),
            (pending.connectionRevision == nil || pending.connectionRevision == operation.connectionRevision),
            (pending.action == nil || pending.action == operation.action),
            (operation.authorizationURL == nil) == (operation.userCode == nil),
            operation.authorizationURL == nil || (operation.action == .start && operation.state == .pending) else {
            authenticationUnconfirmed(connectionID: operation.connectionID)
            return false
        }
        pending.operationID = operation.operationID
        pending.serverInstanceID = operation.serverInstanceID
        pending.connectionRevision = operation.connectionRevision
        pending.action = operation.action
        pending.state = operation.state
        pending.authorizationURL = operation.state == .pending ? operation.authorizationURL : nil
        pending.userCode = operation.state == .pending ? operation.userCode : nil
        pending.reasonMessage = ProviderDisplay.reason(operation.reason)
        authentications[operation.connectionID] = pending
        return true
    }

    public mutating func authenticationUnconfirmed(connectionID: String) {
        authentications[connectionID]?.state = .unknown
        clearAuthorizationChallenge(connectionID: connectionID)
        authentications[connectionID]?.reasonMessage = nil
    }

    public mutating func clearAuthorizationChallenges() {
        for connectionID in Array(authentications.keys) {
            clearAuthorizationChallenge(connectionID: connectionID)
        }
    }

    private mutating func clearAuthorizationChallenge(connectionID: String) {
        authentications[connectionID]?.authorizationURL = nil
        authentications[connectionID]?.userCode = nil
    }

    public mutating func authenticationRejected(connectionID: String, requestID: String) {
        guard let pending = authentications[connectionID], pending.startRequestID == requestID,
            pending.operationID == nil else { return }
        authentications.removeValue(forKey: connectionID)
    }

    public mutating func beginSetup(providerID: ProviderID, resourceID: String, requestID: String) -> Bool {
        guard setups[providerID]?.unresolved != true else { return false }
        setups[providerID] = Setup(
            providerID: providerID, startRequestID: requestID, resourceID: resourceID, state: .queued)
        return true
    }

    public mutating func setupAccepted(providerID: ProviderID, requestID: String, jobID: String) {
        guard var pending = setups[providerID], pending.startRequestID == requestID else { return }
        pending.jobID = jobID
        setups[providerID] = pending
    }

    public mutating func setupUnconfirmed(providerID: ProviderID) { setups[providerID]?.state = .unknown }

    public mutating func setupRejected(providerID: ProviderID, requestID: String) {
        guard let pending = setups[providerID], pending.startRequestID == requestID,
            pending.jobID == nil else { return }
        setups.removeValue(forKey: providerID)
    }

    @discardableResult
    public mutating func receive(_ job: Job, providerID: ProviderID) -> Bool {
        guard var pending = setups[providerID], pending.jobID == job.jobID,
            job.kind == "provider_setup", let state = ProviderSetupState(rawValue: job.status) else { return false }
        pending.state = state
        setups[providerID] = pending
        return true
    }

    public mutating func observe(connections: [ProviderConnection]) {
        let connectionIDs = Set(connections.map(\.connectionID))
        for connectionID in Array(authentications.keys) where !connectionIDs.contains(connectionID) {
            clearAuthorizationChallenge(connectionID: connectionID)
        }
        for connection in connections {
            guard let active = connection.activeAuth else {
                clearAuthorizationChallenge(connectionID: connection.connectionID)
                continue
            }
            if var pending = authentications[connection.connectionID], pending.unresolved {
                guard pending.startRequestID == active.startRequestID else {
                    clearAuthorizationChallenge(connectionID: connection.connectionID)
                    continue
                }
                guard pending.serverInstanceID == nil || pending.serverInstanceID == active.serverInstanceID,
                    pending.operationID == nil || pending.operationID == active.operationID,
                    pending.connectionRevision == nil || pending.connectionRevision == connection.revision else {
                    authenticationUnconfirmed(connectionID: connection.connectionID)
                    continue
                }
                pending.operationID = active.operationID
                pending.serverInstanceID = active.serverInstanceID
                pending.connectionRevision = connection.revision
                if active.state != pending.state { pending.reasonMessage = nil }
                pending.state = active.state
                if active.state != .pending {
                    pending.authorizationURL = nil
                    pending.userCode = nil
                }
                authentications[connection.connectionID] = pending
                continue
            }
            authentications[connection.connectionID] = Authentication(
                connectionID: connection.connectionID, startRequestID: active.startRequestID,
                operationID: active.operationID, serverInstanceID: active.serverInstanceID,
                connectionRevision: connection.revision, state: active.state)
        }
        // An absent active_auth must not resolve a lost receipt or a restarted operation.
    }

    public mutating func observe(providers: [ProviderDescriptor]) {
        for provider in providers {
            let candidates = [provider.runtime.activeSetup, provider.runtime.lastSetup].compactMap { $0 }
            if let pending = setups[provider.providerID], pending.unresolved {
                if let match = candidates.first(where: { $0.startRequestID == pending.startRequestID }) {
                    guard match.resourceID == pending.resourceID,
                        pending.jobID == nil || pending.jobID == match.jobID else {
                        setupUnconfirmed(providerID: provider.providerID)
                        continue
                    }
                    setups[provider.providerID] = Setup(
                        providerID: provider.providerID, startRequestID: match.startRequestID,
                        resourceID: match.resourceID, jobID: match.jobID, state: match.state)
                }
            } else if let existing = candidates.first {
                setups[provider.providerID] = Setup(
                    providerID: provider.providerID, startRequestID: existing.startRequestID,
                    resourceID: existing.resourceID, jobID: existing.jobID, state: existing.state)
            }
        }
    }
}
