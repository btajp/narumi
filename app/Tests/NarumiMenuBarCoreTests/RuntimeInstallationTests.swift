import XCTest

@testable import NarumiMenuBarCore

final class RuntimeInstallationTests: XCTestCase {
    private let fm = FileManager.default
    private let oldManifest = Data("{ legacy marker bytes }\n".utf8)
    private var newManifest: Data {
        get throws {
            let encoder = JSONEncoder()
            encoder.outputFormatting = [.sortedKeys]
            return try encoder.encode(RuntimeManifest(
                appVersion: "2.0.0", python: "3.13", uvVersion: "0.12.6",
                wheels: ["narumi-2.0.0.whl": "abcd"], requirementsSHA256: "1234"))
        }
    }
    private enum Interrupted: Error { case processExited }

    private func fixture(oldVenv: Bool = true, oldMarker: Bool = true) throws -> RuntimePaths {
        let root = fm.temporaryDirectory.appendingPathComponent("narumi-runtime-\(UUID().uuidString)")
        try fm.createDirectory(at: root, withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: root) }
        let paths = RuntimePaths(dataRoot: root)
        try fm.createDirectory(at: paths.root, withIntermediateDirectories: true)
        if oldVenv { try writeVenv(paths.venv, value: "old") }
        if oldMarker { try oldManifest.write(to: paths.installedManifest) }
        try writeVenv(paths.venvStaging, value: "new")
        // Files outside runtime are intentionally present throughout every recovery.
        try Data("user preferences".utf8).write(to: root.appendingPathComponent("preferences.json"))
        let meetings = root.appendingPathComponent("meetings")
        try fm.createDirectory(at: meetings, withIntermediateDirectories: true)
        try Data("original meeting".utf8).write(to: meetings.appendingPathComponent("sentinel"))
        return paths
    }

    private func writeVenv(_ path: URL, value: String) throws {
        try fm.createDirectory(at: path.appendingPathComponent("bin"), withIntermediateDirectories: true)
        try Data(value.utf8).write(to: path.appendingPathComponent("bin/narumi-server"))
    }

    private func contents(_ path: URL) throws -> String {
        String(decoding: try Data(contentsOf: path.appendingPathComponent("bin/narumi-server")), as: UTF8.self)
    }

    private func assertRestored(_ paths: RuntimePaths, oldVenv: Bool = true, oldMarker: Bool = true) throws {
        if oldVenv {
            XCTAssertEqual(try contents(paths.venv), "old")
        } else {
            XCTAssertFalse(fm.fileExists(atPath: paths.venv.path))
        }
        if oldMarker {
            XCTAssertEqual(try Data(contentsOf: paths.installedManifest), oldManifest)
        } else {
            XCTAssertFalse(fm.fileExists(atPath: paths.installedManifest.path))
        }
        XCTAssertFalse(fm.fileExists(atPath: paths.transactionJournal.path))
        XCTAssertFalse(fm.fileExists(atPath: paths.venvPrevious.path))
        let root = paths.root.deletingLastPathComponent()
        XCTAssertEqual(try Data(contentsOf: root.appendingPathComponent("preferences.json")), Data("user preferences".utf8))
        XCTAssertEqual(try Data(contentsOf: root.appendingPathComponent("meetings/sentinel")), Data("original meeting".utf8))
    }

    private func interrupt(_ installation: RuntimeInstallation, at point: RuntimeInstallation.Checkpoint) {
        installation.checkpoint = { checkpoint in
            if checkpoint == point { throw Interrupted.processExited }
        }
    }

    func testActivationKeepsPreviousVenvAndExactMarkerUntilReadiness() throws {
        let paths = try fixture()
        let installation = RuntimeInstallation(paths: paths)
        try installation.activate(manifest: newManifest)
        XCTAssertEqual(try contents(paths.venv), "new")
        XCTAssertEqual(try contents(paths.venvPrevious), "old")
        XCTAssertEqual(try Data(contentsOf: paths.installedManifest), oldManifest)
        XCTAssertTrue(fm.fileExists(atPath: paths.transactionJournal.path))
        try installation.commit()
        XCTAssertEqual(try contents(paths.venv), "new")
        XCTAssertEqual(try Data(contentsOf: paths.installedManifest), try newManifest)
        XCTAssertFalse(fm.fileExists(atPath: paths.venvPrevious.path))
        XCTAssertFalse(fm.fileExists(atPath: paths.transactionJournal.path))
        XCTAssertEqual(try installation.recover(), .none)
    }

    func testFailedServerReadinessRestoresOldArtifactsWithoutStartingOldServer() throws {
        let paths = try fixture()
        let installation = RuntimeInstallation(paths: paths)
        try installation.activate(manifest: newManifest)
        // The fake startup never reports readiness, so commit is not called. Recovery has
        // no process-launch facility and therefore cannot silently run the previous version.
        XCTAssertEqual(try installation.recover(), .rolledBack)
        try assertRestored(paths)
    }

    func testInterruptedActivationRecoversAtEveryRenameBoundary() throws {
        for point in [RuntimeInstallation.Checkpoint.journalWritten, .previousVenvMoved, .candidateActivated] {
            for hasPrevious in [true, false] {
                let paths = try fixture(oldVenv: hasPrevious, oldMarker: hasPrevious)
                let installation = RuntimeInstallation(paths: paths)
                interrupt(installation, at: point)
                XCTAssertThrowsError(try installation.activate(manifest: newManifest), "\(point)")
                let nextLaunch = RuntimeInstallation(paths: paths)
                XCTAssertEqual(try nextLaunch.recover(), .rolledBack, "\(point)")
                try assertRestored(paths, oldVenv: hasPrevious, oldMarker: hasPrevious)
                XCTAssertEqual(try nextLaunch.recover(), .none)
            }
        }
    }

    func testInterruptedRollbackResumesAtEveryBoundary() throws {
        let points: [RuntimeInstallation.Checkpoint] = [
            .rollbackRecorded, .candidateRemoved, .previousVenvRestored, .previousManifestRestored,
        ]
        for point in points {
            let paths = try fixture()
            let installation = RuntimeInstallation(paths: paths)
            try installation.activate(manifest: newManifest)
            interrupt(installation, at: point)
            XCTAssertThrowsError(try installation.recover(), "\(point)")
            XCTAssertEqual(try RuntimeInstallation(paths: paths).recover(), .rolledBack)
            try assertRestored(paths)
        }
    }

    func testInterruptedMarkerWriteBeforeCommitRestoresPreviousBytes() throws {
        let paths = try fixture()
        let installation = RuntimeInstallation(paths: paths)
        try installation.activate(manifest: newManifest)
        interrupt(installation, at: .candidateManifestWritten)
        XCTAssertThrowsError(try installation.commit())
        XCTAssertEqual(try Data(contentsOf: paths.installedManifest), try newManifest)
        XCTAssertEqual(try RuntimeInstallation(paths: paths).recover(), .rolledBack)
        try assertRestored(paths)
    }

    func testInterruptedCommittedCleanupKeepsVerifiedRuntime() throws {
        for point in [RuntimeInstallation.Checkpoint.commitRecorded, .previousVenvRemoved] {
            let paths = try fixture()
            let installation = RuntimeInstallation(paths: paths)
            try installation.activate(manifest: newManifest)
            interrupt(installation, at: point)
            XCTAssertThrowsError(try installation.commit())
            let nextLaunch = RuntimeInstallation(paths: paths)
            XCTAssertEqual(try nextLaunch.recover(), .completedCommit)
            XCTAssertEqual(try contents(paths.venv), "new")
            XCTAssertEqual(try Data(contentsOf: paths.installedManifest), try newManifest)
            XCTAssertFalse(fm.fileExists(atPath: paths.venvPrevious.path))
            XCTAssertFalse(fm.fileExists(atPath: paths.transactionJournal.path))
            XCTAssertEqual(try nextLaunch.recover(), .none)
        }
    }

    func testRollbackPreservesMarkerEvenWhenOldVenvWasAlreadyMissing() throws {
        let paths = try fixture(oldVenv: false)
        let installation = RuntimeInstallation(paths: paths)
        try installation.activate(manifest: newManifest)
        XCTAssertEqual(try installation.recover(), .rolledBack)
        try assertRestored(paths, oldVenv: false)
    }

    func testRollbackPreservesMissingMarkerEvenWhenOldVenvExists() throws {
        let paths = try fixture(oldMarker: false)
        let installation = RuntimeInstallation(paths: paths)
        try installation.activate(manifest: newManifest)
        XCTAssertEqual(try installation.recover(), .rolledBack)
        try assertRestored(paths, oldMarker: false)
    }

    func testInvalidCandidateDoesNotChangeOldRuntime() throws {
        let paths = try fixture()
        let installation = RuntimeInstallation(paths: paths)
        XCTAssertThrowsError(try installation.activate(manifest: Data("broken".utf8)))
        try assertRestored(paths)
        try fm.removeItem(at: paths.venvStaging)
        XCTAssertThrowsError(try installation.activate(manifest: newManifest))
        try assertRestored(paths)
    }

    func testCommitBeforeActivationFailsWithoutLosingOldRuntime() throws {
        let paths = try fixture()
        let installation = RuntimeInstallation(paths: paths)
        interrupt(installation, at: .journalWritten)
        XCTAssertThrowsError(try installation.activate(manifest: newManifest))
        installation.checkpoint = nil
        XCTAssertThrowsError(try installation.commit())
        XCTAssertEqual(try installation.recover(), .rolledBack)
        try assertRestored(paths)
    }

    func testCorruptRecoveryJournalDoesNotDeleteEitherRuntime() throws {
        let paths = try fixture()
        let installation = RuntimeInstallation(paths: paths)
        try installation.activate(manifest: newManifest)
        try Data("not a journal".utf8).write(to: paths.transactionJournal)
        XCTAssertThrowsError(try RuntimeInstallation(paths: paths).recover())
        XCTAssertEqual(try contents(paths.venv), "new")
        XCTAssertEqual(try contents(paths.venvPrevious), "old")
        XCTAssertEqual(try Data(contentsOf: paths.installedManifest), oldManifest)
    }

    func testMissingBackupIsNotMistakenForSuccessfulRollback() throws {
        let paths = try fixture()
        let installation = RuntimeInstallation(paths: paths)
        try installation.activate(manifest: newManifest)
        // Simulate unrelated file removal; guessing could label the new venv as the old one.
        try fm.removeItem(at: paths.venvPrevious)
        XCTAssertThrowsError(try RuntimeInstallation(paths: paths).recover())
        XCTAssertEqual(try contents(paths.venv), "new")
        XCTAssertTrue(fm.fileExists(atPath: paths.transactionJournal.path))
    }

    func testOrphanedBackupWithoutJournalIsNeverOverwritten() throws {
        let paths = try fixture()
        try writeVenv(paths.venvPrevious, value: "unidentified backup")
        let installation = RuntimeInstallation(paths: paths)
        XCTAssertThrowsError(try installation.recover())
        XCTAssertThrowsError(try installation.activate(manifest: newManifest))
        XCTAssertEqual(try contents(paths.venvPrevious), "unidentified backup")
        XCTAssertEqual(try contents(paths.venv), "old")
    }
}
