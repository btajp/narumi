import XCTest

@testable import NarumiMenuBarCore

final class RuntimeOwnedProcessExitTests: XCTestCase {
    private let child = RuntimeSyncOwnership.Identity(
        pid: 1234, startedSeconds: 100, startedMicroseconds: 20, processGroup: 1234)

    private final class Probe {
        struct Unknown: Error {}
        var current: RuntimeSyncOwnership.Identity?
        var groupAlive = false
        var unknownIdentity = false
        var unknownGroup = false
        var unknownBoot = false
        var onGroupCheck: (() throws -> Void)?

        var inspection: RuntimeSyncOwnership.Inspection {
            .init(
                bootSession: {
                    if self.unknownBoot { throw Unknown() }
                    return "same-boot"
                },
                identity: { _ in
                    if self.unknownIdentity { throw Unknown() }
                    return self.current
                },
                groupExists: { _ in
                    try self.onGroupCheck?()
                    if self.unknownGroup { throw Unknown() }
                    return self.groupAlive
                })
        }
    }

    private struct Fixture {
        let owner: RuntimeSyncOwnership
        let probe: Probe
        let paths: RuntimePaths
        let record: RuntimeSyncOwnership.Record

        func write(_ record: RuntimeSyncOwnership.Record) throws {
            try JSONEncoder().encode(record).write(to: owner.recordURL)
        }
    }

    private func fixture(currentToken: String? = "owned-request") throws -> Fixture {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent("narumi-owned-exit-\(UUID().uuidString)")
        let paths = RuntimePaths(dataRoot: root)
        try FileManager.default.createDirectory(at: paths.root, withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: root) }
        let probe = Probe()
        let owner = RuntimeSyncOwnership(paths: paths, inspection: probe.inspection, currentToken: currentToken)
        let record = RuntimeSyncOwnership.Record(
            token: "owned-request", bootSession: "same-boot",
            app: .init(pid: 12, startedSeconds: 80, startedMicroseconds: 10, processGroup: 12), child: child)
        let fixture = Fixture(owner: owner, probe: probe, paths: paths, record: record)
        try fixture.write(record)
        return fixture
    }

    private func capture(_ fixture: Fixture) throws -> RuntimeSyncOwnership.OwnedProcessToken {
        try XCTUnwrap(fixture.owner.captureOwnedProcessToken(expectedPID: child.pid))
    }

    func testDeadOwnedTreeIsConfirmedOnlyOnceByStrictWrapper() throws {
        let fixture = try fixture()
        let token = try capture(fixture)
        XCTAssertEqual(token.processID, child.pid)
        XCTAssertTrue(fixture.owner.confirmOwnedProcessTreeExited(token))
        XCTAssertFalse(FileManager.default.fileExists(atPath: fixture.owner.recordURL.path))
        XCTAssertFalse(fixture.owner.confirmOwnedProcessTreeExited(token))
        XCTAssertNil(fixture.owner.captureOwnedProcessToken(expectedPID: child.pid))
    }

    func testDurableRecordAloneDoesNotConferOwnership() throws {
        let fixture = try fixture(currentToken: nil)
        XCTAssertNil(fixture.owner.captureOwnedProcessToken(expectedPID: child.pid))
        XCTAssertTrue(FileManager.default.fileExists(atPath: fixture.owner.recordURL.path))
    }

    func testCaptureRejectsWrongPIDOrUnrecordedChild() throws {
        let fixture = try fixture()
        XCTAssertNil(fixture.owner.captureOwnedProcessToken(expectedPID: child.pid + 1))
        var intent = fixture.record
        intent.child = nil
        try fixture.write(intent)
        XCTAssertNil(fixture.owner.captureOwnedProcessToken(expectedPID: child.pid))
    }

    func testDifferentCurrentTokenCannotCaptureRecord() throws {
        let fixture = try fixture(currentToken: "another-request")
        XCTAssertNil(fixture.owner.captureOwnedProcessToken(expectedPID: child.pid))
    }

    func testLiveAndUnknownProcessesOrGroupsKeepTheRecord() throws {
        let cases: [(String, (Probe) -> Void)] = [
            ("server alive", { $0.current = self.child }),
            ("only group child alive", { $0.groupAlive = true }),
            ("identity unavailable", { $0.unknownIdentity = true }),
            ("group unavailable or EPERM", { $0.unknownGroup = true }),
            ("boot identity unavailable", { $0.unknownBoot = true }),
        ]
        for (name, configure) in cases {
            let fixture = try fixture()
            let token = try capture(fixture)
            configure(fixture.probe)
            XCTAssertFalse(fixture.owner.confirmOwnedProcessTreeExited(token), name)
            XCTAssertTrue(FileManager.default.fileExists(atPath: fixture.owner.recordURL.path), name)
        }
    }

    func testChangedRecordDoesNotProveCapturedTreeExit() throws {
        let cases: [(String, (inout RuntimeSyncOwnership.Record) -> Void)] = [
            ("token", { $0.token = "another-request" }),
            ("child PID", { $0.child?.pid += 1 }),
            ("child start time", { $0.child?.startedMicroseconds += 1 }),
            ("child group", { $0.child?.processGroup += 1 }),
            ("app identity", { $0.app.startedMicroseconds += 1 }),
            ("boot session", { $0.bootSession = "another-boot" }),
            ("format", { $0.formatVersion = 2 }),
            ("missing child", { $0.child = nil }),
        ]
        for (name, mutate) in cases {
            let fixture = try fixture()
            let token = try capture(fixture)
            var changed = fixture.record
            mutate(&changed)
            try fixture.write(changed)
            XCTAssertFalse(fixture.owner.confirmOwnedProcessTreeExited(token), name)
            XCTAssertEqual(
                try JSONDecoder().decode(RuntimeSyncOwnership.Record.self, from: Data(contentsOf: fixture.owner.recordURL)),
                changed, name)
        }
    }

    func testMissingRecordFailsStrictlyWhileLegacyIdleStillSucceeds() throws {
        let fixture = try fixture()
        let token = try capture(fixture)
        try FileManager.default.removeItem(at: fixture.owner.recordURL)
        XCTAssertNoThrow(try fixture.owner.requireIdle())
        XCTAssertFalse(fixture.owner.confirmOwnedProcessTreeExited(token))
        XCTAssertNil(fixture.owner.captureOwnedProcessToken(expectedPID: child.pid))
    }

    func testCorruptRecordIsNeitherConfirmedNorRemoved() throws {
        let fixture = try fixture()
        let token = try capture(fixture)
        let corrupt = Data("broken record".utf8)
        try corrupt.write(to: fixture.owner.recordURL)
        XCTAssertFalse(fixture.owner.confirmOwnedProcessTreeExited(token))
        XCTAssertNil(fixture.owner.captureOwnedProcessToken(expectedPID: child.pid))
        XCTAssertEqual(try Data(contentsOf: fixture.owner.recordURL), corrupt)
    }

    func testRecordChangedDuringInspectionIsNotRemoved() throws {
        let fixture = try fixture()
        let token = try capture(fixture)
        var changed = fixture.record
        changed.child?.startedMicroseconds += 1
        let recordURL = fixture.owner.recordURL
        fixture.probe.onGroupCheck = { try JSONEncoder().encode(changed).write(to: recordURL) }
        XCTAssertFalse(fixture.owner.confirmOwnedProcessTreeExited(token))
        XCTAssertEqual(
            try JSONDecoder().decode(RuntimeSyncOwnership.Record.self, from: Data(contentsOf: fixture.owner.recordURL)),
            changed)
    }

    func testPIDReuseAndGoneGroupProveTheOriginalProcessExited() throws {
        let fixture = try fixture()
        let token = try capture(fixture)
        var reused = child
        reused.startedMicroseconds += 1
        fixture.probe.current = reused
        XCTAssertTrue(fixture.owner.confirmOwnedProcessTreeExited(token))
    }

    func testAnotherOwnerInstanceCannotConfirmEvenWithSameDurableToken() throws {
        let fixture = try fixture()
        let token = try capture(fixture)
        let replacement = RuntimeSyncOwnership(
            paths: fixture.paths, inspection: fixture.probe.inspection, currentToken: fixture.record.token)
        XCTAssertFalse(replacement.confirmOwnedProcessTreeExited(token))
        XCTAssertTrue(FileManager.default.fileExists(atPath: fixture.owner.recordURL.path))
    }

    func testFinishedOwnerCannotRecoverAuthorityFromRestoredFile() throws {
        let fixture = try fixture()
        let token = try capture(fixture)
        try fixture.owner.finish()
        try fixture.write(fixture.record)
        XCTAssertFalse(fixture.owner.confirmOwnedProcessTreeExited(token))
        XCTAssertNil(fixture.owner.captureOwnedProcessToken(expectedPID: child.pid))
        XCTAssertNoThrow(try fixture.owner.finish())
        XCTAssertTrue(FileManager.default.fileExists(atPath: fixture.owner.recordURL.path))
    }
}
