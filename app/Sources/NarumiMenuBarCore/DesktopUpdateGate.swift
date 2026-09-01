import Foundation

/// Defers update installation until the desktop app can safely terminate.
/// UI callbacks and the final termination decision remain the adapter's responsibility.
public struct DesktopUpdateGate: Equatable, Sendable {
    public enum Phase: Equatable, Sendable {
        case idle
        case waiting
        case validating
        case installing
    }

    public struct Token: Equatable, Sendable {
        fileprivate let generation: UInt64
    }

    public private(set) var phase: Phase = .idle
    private var generation: UInt64 = 0
    private var activeToken: Token?

    public init() {}

    public var installing: Bool { phase == .installing }

    /// Feed discovery and downloads must remain available even when recording state is
    /// unavailable. Only the final installation/relaunch is gated by application safety.
    public func canCheckForUpdates(updaterCanCheck: Bool) -> Bool {
        updaterCanCheck && !installing
    }

    public mutating func deferInstallation() {
        invalidateValidation()
        phase = .waiting
    }

    public mutating func beginValidation(blocked: Bool) -> Token? {
        guard phase == .waiting, !blocked else { return nil }
        generation &+= 1
        let token = Token(generation: generation)
        activeToken = token
        phase = .validating
        return token
    }

    /// Recheck blocking work after asynchronous validation before allowing installation.
    @discardableResult
    public mutating func finishValidation(_ token: Token, blocked: Bool) -> Bool {
        guard phase == .validating, activeToken == token else { return false }
        invalidateValidation()
        phase = blocked ? .waiting : .installing
        return !blocked
    }

    public mutating func installationTerminationDenied() {
        guard phase == .installing else { return }
        invalidateValidation()
        phase = .waiting
    }

    public mutating func finishCycle() {
        invalidateValidation()
        phase = .idle
    }

    private mutating func invalidateValidation() {
        generation &+= 1
        activeToken = nil
    }
}
