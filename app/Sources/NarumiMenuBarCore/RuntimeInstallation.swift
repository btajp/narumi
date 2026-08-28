import Foundation

/// A recoverable swap of runtime artifacts, never of the user's meeting data.
///
/// `activate` keeps the old marker and renames the old venv to `venv.previous`. Only after
/// the launched server has answered with the expected identity may the caller `commit`.
/// Before using this helper the caller must ensure no server is using these paths.
public final class RuntimeInstallation {
    public enum Recovery: Equatable {
        case none
        case rolledBack
        case completedCommit
    }

    struct Journal: Codable {
        enum Phase: String, Codable { case pending, rollingBack, committed }
        var formatVersion = 1
        var phase: Phase
        var hadVenv: Bool
        // Preserve exact bytes, including an unreadable-as-JSON legacy marker.
        var previousManifest: Data?
        var candidateManifest: Data
    }

    /// Tests interrupt after each durable operation, then recover using a fresh instance.
    enum Checkpoint: CaseIterable {
        case journalWritten, previousVenvMoved, candidateActivated
        case candidateManifestWritten, commitRecorded, previousVenvRemoved
        case rollbackRecorded, candidateRemoved, previousVenvRestored, previousManifestRestored
    }

    struct Failure: LocalizedError {
        let message: String
        var errorDescription: String? { "ランタイムの切り替えに失敗: \(message)" }
    }

    public let paths: RuntimePaths
    var checkpoint: ((Checkpoint) throws -> Void)?
    private let fm = FileManager.default

    public init(paths: RuntimePaths) {
        self.paths = paths
    }

    /// Journal before the first rename. A failed operation deliberately leaves the journal
    /// in place: the caller stops the candidate process before invoking `recover`.
    public func activate(manifest: Data) throws {
        _ = try JSONDecoder().decode(RuntimeManifest.self, from: manifest)
        guard !exists(paths.transactionJournal), !exists(paths.venvPrevious) else {
            throw Failure(message: "未復旧の記録または旧環境があります。先に復旧してください")
        }
        guard exists(paths.venvStaging) else {
            throw Failure(message: "準備済みの新環境がありません")
        }
        let oldMarker = exists(paths.installedManifest) ? try Data(contentsOf: paths.installedManifest) : nil
        let journal = Journal(
            phase: .pending, hadVenv: exists(paths.venv), previousManifest: oldMarker,
            candidateManifest: manifest)
        try write(journal)
        try checkpoint?(.journalWritten)
        if journal.hadVenv {
            try fm.moveItem(at: paths.venv, to: paths.venvPrevious)
        }
        try checkpoint?(.previousVenvMoved)
        try fm.moveItem(at: paths.venvStaging, to: paths.venv)
        try checkpoint?(.candidateActivated)
    }

    /// Call only after the new server has passed readiness AND identity checks. The new
    /// marker is atomic, and its old bytes stay in the journal until commit is durable.
    public func commit() throws {
        guard exists(paths.transactionJournal) else { return }
        var journal = try read()
        if journal.phase == .committed {
            try finishCommit()
            return
        }
        guard journal.phase == .pending, exists(paths.venv), !exists(paths.venvStaging),
            !journal.hadVenv || exists(paths.venvPrevious)
        else {
            throw Failure(message: "新環境への切り替えが完了していません")
        }
        try journal.candidateManifest.write(to: paths.installedManifest, options: .atomic)
        try checkpoint?(.candidateManifestWritten)
        journal.phase = .committed
        try write(journal)
        try checkpoint?(.commitRecorded)
        try finishCommit()
    }

    /// Safe to repeat after interruption, including an interruption during rollback itself.
    /// A committed journal is only cleanup work; every uncommitted journal restores the old
    /// venv and marker. Recovery never starts the old server as a silent fallback.
    @discardableResult
    public func recover() throws -> Recovery {
        guard exists(paths.transactionJournal) else {
            guard !exists(paths.venvPrevious) else {
                throw Failure(message: "復旧記録のない旧環境があります。自動削除は行いません")
            }
            return .none
        }
        var journal = try read()
        if journal.phase == .committed {
            try finishCommit()
            return .completedCommit
        }
        if journal.phase == .pending {
            // Before the first rename both venv and staging exist. After it the backup
            // must exist. A missing backup in any other state is not safe to guess about.
            if journal.hadVenv, !exists(paths.venvPrevious),
                !(exists(paths.venv) && exists(paths.venvStaging))
            {
                throw Failure(message: "復元に必要な旧環境が見つかりません")
            }
            journal.phase = .rollingBack
            try write(journal)
            try checkpoint?(.rollbackRecorded)
        }
        if journal.hadVenv {
            if exists(paths.venvPrevious) {
                try removeIfPresent(paths.venv)
                try checkpoint?(.candidateRemoved)
                try fm.moveItem(at: paths.venvPrevious, to: paths.venv)
                try checkpoint?(.previousVenvRestored)
            } else if !exists(paths.venv) {
                throw Failure(message: "復元に必要な旧環境が見つかりません")
            }
        } else {
            guard !exists(paths.venvPrevious) else {
                throw Failure(message: "復旧記録と旧環境が一致しません")
            }
            try removeIfPresent(paths.venv)
            try checkpoint?(.candidateRemoved)
        }
        if let previous = journal.previousManifest {
            try previous.write(to: paths.installedManifest, options: .atomic)
        } else {
            try removeIfPresent(paths.installedManifest)
        }
        try checkpoint?(.previousManifestRestored)
        try fm.removeItem(at: paths.transactionJournal)
        return .rolledBack
    }

    private func finishCommit() throws {
        try removeIfPresent(paths.venvPrevious)
        try checkpoint?(.previousVenvRemoved)
        try fm.removeItem(at: paths.transactionJournal)
    }

    private func read() throws -> Journal {
        let journal = try JSONDecoder().decode(Journal.self, from: Data(contentsOf: paths.transactionJournal))
        guard journal.formatVersion == 1 else {
            throw Failure(message: "対応していない復旧記録です。自動削除は行いません")
        }
        return journal
    }

    private func write(_ journal: Journal) throws {
        try JSONEncoder().encode(journal).write(to: paths.transactionJournal, options: .atomic)
    }

    private func exists(_ url: URL) -> Bool {
        // Unlike fileExists, this also sees broken symlinks without following them.
        (try? fm.attributesOfItem(atPath: url.path)) != nil
    }

    private func removeIfPresent(_ url: URL) throws {
        if exists(url) { try fm.removeItem(at: url) }
    }
}
