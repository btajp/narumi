import Foundation

/// Permission-result recovery is permitted only for the same bundled-server ownership
/// generation. This value never probes or signals processes; the launcher supplies facts.
public struct OwnedServerRecovery {
    public enum Source: Equatable, Sendable { case bundled, repository, external }

    public struct Context: Equatable, Sendable {
        public var source: Source
        public var runtimeRoot: URL
        public var leaseIdentity: ObjectIdentifier?
        public var ownershipIdentity: ObjectIdentifier?

        public init(
            source: Source, runtimeRoot: URL, leaseIdentity: ObjectIdentifier?,
            ownershipIdentity: ObjectIdentifier?
        ) {
            self.source = source
            self.runtimeRoot = runtimeRoot
            self.leaseIdentity = leaseIdentity
            self.ownershipIdentity = ownershipIdentity
        }
    }

    public struct Token: Equatable, Sendable {
        public var serverPID: Int32 { process.processID }
        fileprivate let generation: UUID
        fileprivate let context: Context
        fileprivate let process: RuntimeSyncOwnership.OwnedProcessToken
    }

    private var generation = UUID()
    private var capturedToken: Token?
    private var confirmedToken: Token?

    public init() {}

    /// A new start/stop or discarded/replaced runtime context invalidates earlier proof,
    /// even when a new object happens to reuse the same memory address or PID.
    public mutating func invalidate() {
        generation = UUID()
        capturedToken = nil
        confirmedToken = nil
    }

    public mutating func capture(
        _ process: RuntimeSyncOwnership.OwnedProcessToken?, context: Context?, syncing: Bool
    ) -> Token? {
        guard !syncing, let context, context.source == .bundled, context.leaseIdentity != nil,
            let process,
            process.matches(ownerIdentity: context.ownershipIdentity, runtimeRoot: context.runtimeRoot)
        else { return nil }
        let token = Token(generation: generation, context: context, process: process)
        if capturedToken != token { confirmedToken = nil }
        capturedToken = token
        return token
    }

    public mutating func confirm(
        _ token: Token, context: Context?, busy: Bool, hasServerProcess: Bool, hasSyncProcess: Bool,
        inspect: (RuntimeSyncOwnership.OwnedProcessToken) -> Bool
    ) -> Bool {
        guard !busy, !hasServerProcess, !hasSyncProcess,
            let context, context.source == .bundled, context.leaseIdentity != nil,
            token.generation == generation, token.context == context, capturedToken == token
        else { return false }
        // The strict inspection removes the durable record. Retain its successful proof
        // only while every context/generation guard above still holds.
        if confirmedToken == token { return true }
        guard inspect(token.process) else { return false }
        confirmedToken = token
        return true
    }
}
