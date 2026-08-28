import Foundation

/// Jobs known to the desktop app, including IDs whose first status request is pending.
/// Missing responses are not evidence that a job stopped running.
public struct DesktopJobState: Equatable, Sendable {
    public struct RefreshToken: Equatable, Sendable {
        public let ids: [String]
        fileprivate let revision: UInt64
    }

    public private(set) var trackedIDs: [String] = []
    private var snapshots: [String: Job] = [:]
    private var revision: UInt64 = 0
    private var activeToken: RefreshToken?

    public init() {}

    /// Successful status responses in tracking order (newest tracked ID first).
    public var jobs: [Job] {
        trackedIDs.compactMap { snapshots[$0] }
    }

    /// Unknown IDs and unrecognized statuses block updates until resolved explicitly.
    public var knownActiveIDs: Set<String> {
        Set(trackedIDs.filter { id in
            guard let job = snapshots[id] else { return true }
            return !Self.isFinished(job)
        })
    }

    public var activeCount: Int { knownActiveIDs.count }

    public mutating func track(jobID: String) {
        invalidateRefresh()
        guard !trackedIDs.contains(jobID) else { return }
        trackedIDs.insert(jobID, at: 0)
    }

    /// Keep unresolved IDs as well as jobs known to be active.
    public mutating func clearFinished() {
        invalidateRefresh()
        let keep = knownActiveIDs
        trackedIDs.removeAll { !keep.contains($0) }
        snapshots = snapshots.filter { keep.contains($0.key) }
    }

    /// Call when the connection changes; old responses must not update this state.
    public mutating func invalidateRefresh() {
        revision &+= 1
        activeToken = nil
    }

    public mutating func beginRefresh() -> RefreshToken? {
        guard activeToken == nil, !trackedIDs.isEmpty else { return nil }
        revision &+= 1
        let token = RefreshToken(ids: trackedIDs, revision: revision)
        activeToken = token
        return token
    }

    /// Apply only successful results. Put confirmed `not_found` IDs in `missingIDs`;
    /// omit transport/query failures from both collections to preserve their state.
    @discardableResult
    public mutating func finishRefresh(
        _ token: RefreshToken, jobs: [Job], missingIDs: Set<String> = []
    ) -> Bool {
        guard activeToken == token else { return false }
        activeToken = nil
        let requestedIDs = Set(token.ids)
        var returnedIDs: Set<String> = []
        for job in jobs where requestedIDs.contains(job.jobID) {
            snapshots[job.jobID] = job
            returnedIDs.insert(job.jobID)
        }
        // A returned status takes precedence over a contradictory missing result.
        let confirmedMissing = missingIDs.intersection(requestedIDs).subtracting(returnedIDs)
        trackedIDs.removeAll { confirmedMissing.contains($0) }
        for id in confirmedMissing {
            snapshots.removeValue(forKey: id)
        }
        return true
    }

    private static func isFinished(_ job: Job) -> Bool {
        switch job.status {
        case "succeeded", "failed", "cancelled": return true
        default: return false
        }
    }
}
