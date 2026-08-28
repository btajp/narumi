import XCTest

@testable import NarumiMenuBarCore

final class OwnedServerRecoveryTests: XCTestCase {
    private final class LeaseIdentity {}
    private final class Liveness { var serverAlive = false }

    private struct Fixture {
        let owner: RuntimeSyncOwnership
        let lease: LeaseIdentity
        let liveness: Liveness
        let context: OwnedServerRecovery.Context
        let processToken: RuntimeSyncOwnership.OwnedProcessToken
    }

    private func fixture() throws -> Fixture {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent("narumi-server-recovery-\(UUID().uuidString)")
        let paths = RuntimePaths(dataRoot: root)
        try FileManager.default.createDirectory(at: paths.root, withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: root) }
        let child = RuntimeSyncOwnership.Identity(
            pid: 1234, startedSeconds: 100, startedMicroseconds: 20, processGroup: 1234)
        let liveness = Liveness()
        let owner = RuntimeSyncOwnership(
            paths: paths,
            inspection: .init(
                bootSession: { "same-boot" }, identity: { _ in liveness.serverAlive ? child : nil },
                groupExists: { _ in false }),
            currentToken: "owned-server")
        let record = RuntimeSyncOwnership.Record(
            token: "owned-server", bootSession: "same-boot",
            app: .init(pid: 12, startedSeconds: 80, startedMicroseconds: 10, processGroup: 12), child: child)
        try JSONEncoder().encode(record).write(to: owner.recordURL)
        let lease = LeaseIdentity()
        let context = OwnedServerRecovery.Context(
            source: .bundled, runtimeRoot: paths.root, leaseIdentity: ObjectIdentifier(lease),
            ownershipIdentity: ObjectIdentifier(owner))
        return Fixture(
            owner: owner, lease: lease, liveness: liveness, context: context,
            processToken: try XCTUnwrap(owner.captureOwnedProcessToken(expectedPID: child.pid)))
    }

    private func token(
        for fixture: Fixture, in recovery: inout OwnedServerRecovery
    ) throws -> OwnedServerRecovery.Token {
        try XCTUnwrap(recovery.capture(fixture.processToken, context: fixture.context, syncing: false))
    }

    func testOnlyMatchingBundledOwnershipCanBeCaptured() throws {
        let fixture = try fixture()
        let otherIdentity = LeaseIdentity()
        var wrongRoot = fixture.context
        wrongRoot.runtimeRoot = wrongRoot.runtimeRoot.appendingPathComponent("other")
        var cases: [(String, OwnedServerRecovery.Context?)] = [("missing context", nil), ("root", wrongRoot)]
        for source in [OwnedServerRecovery.Source.repository, .external] {
            var context = fixture.context
            context.source = source
            cases.append(("unowned source \(source)", context))
        }
        var missingLease = fixture.context
        missingLease.leaseIdentity = nil
        cases.append(("missing lease", missingLease))
        var missingOwner = fixture.context
        missingOwner.ownershipIdentity = nil
        cases.append(("missing owner", missingOwner))
        var differentOwner = fixture.context
        differentOwner.ownershipIdentity = ObjectIdentifier(otherIdentity)
        cases.append(("different owner", differentOwner))
        for (name, context) in cases {
            var recovery = OwnedServerRecovery()
            XCTAssertNil(recovery.capture(fixture.processToken, context: context, syncing: false), name)
        }
        var recovery = OwnedServerRecovery()
        XCTAssertNil(recovery.capture(fixture.processToken, context: fixture.context, syncing: true))
        XCTAssertNil(recovery.capture(nil, context: fixture.context, syncing: false))
        XCTAssertEqual(try token(for: fixture, in: &recovery).serverPID, 1234)
    }

    func testChangedContextNeverInspectsOrUsesCachedProof() throws {
        let fixture = try fixture()
        var recovery = OwnedServerRecovery()
        let token = try token(for: fixture, in: &recovery)
        XCTAssertTrue(recovery.confirm(
            token, context: fixture.context, busy: false, hasServerProcess: false, hasSyncProcess: false,
            inspect: fixture.owner.confirmOwnedProcessTreeExited))
        let otherIdentity = LeaseIdentity()
        let mutations: [(String, (inout OwnedServerRecovery.Context) -> Void)] = [
            ("external", { $0.source = .external }),
            ("repository", { $0.source = .repository }),
            ("missing lease", { $0.leaseIdentity = nil }),
            ("replaced lease", { $0.leaseIdentity = ObjectIdentifier(otherIdentity) }),
            ("missing owner", { $0.ownershipIdentity = nil }),
            ("replaced owner", { $0.ownershipIdentity = ObjectIdentifier(otherIdentity) }),
            ("runtime root", { $0.runtimeRoot = $0.runtimeRoot.appendingPathComponent("other") }),
        ]
        var inspected = false
        for (name, mutate) in mutations {
            var context = fixture.context
            mutate(&context)
            XCTAssertFalse(recovery.confirm(
                token, context: context, busy: false, hasServerProcess: false, hasSyncProcess: false,
                inspect: { _ in inspected = true; return true }), name)
        }
        XCTAssertFalse(recovery.confirm(
            token, context: nil, busy: false, hasServerProcess: false, hasSyncProcess: false,
            inspect: { _ in inspected = true; return true }))
        XCTAssertFalse(inspected)
    }

    func testBusyOrPresentProcessBlocksEvenAfterExitWasPreviouslyConfirmed() throws {
        let fixture = try fixture()
        var recovery = OwnedServerRecovery()
        let token = try token(for: fixture, in: &recovery)
        XCTAssertTrue(recovery.confirm(
            token, context: fixture.context, busy: false, hasServerProcess: false, hasSyncProcess: false,
            inspect: fixture.owner.confirmOwnedProcessTreeExited))
        let cases: [(String, Bool, Bool, Bool)] = [
            ("launcher busy", true, false, false),
            ("server reference remains", false, true, false),
            ("sync reference remains", false, false, true),
        ]
        for (name, busy, server, sync) in cases {
            XCTAssertFalse(recovery.confirm(
                token, context: fixture.context, busy: busy, hasServerProcess: server, hasSyncProcess: sync,
                inspect: { _ in XCTFail("must not inspect \(name)"); return true }), name)
        }
    }

    func testLiveOwnedTreeStaysBlockedThenDeadTreeUnlocks() throws {
        let fixture = try fixture()
        var recovery = OwnedServerRecovery()
        let token = try token(for: fixture, in: &recovery)
        fixture.liveness.serverAlive = true
        XCTAssertFalse(recovery.confirm(
            token, context: fixture.context, busy: false, hasServerProcess: false, hasSyncProcess: false,
            inspect: fixture.owner.confirmOwnedProcessTreeExited))
        fixture.liveness.serverAlive = false
        XCTAssertTrue(recovery.confirm(
            token, context: fixture.context, busy: false, hasServerProcess: false, hasSyncProcess: false,
            inspect: fixture.owner.confirmOwnedProcessTreeExited))
        XCTAssertFalse(FileManager.default.fileExists(atPath: fixture.owner.recordURL.path))
        XCTAssertTrue(recovery.confirm(
            token, context: fixture.context, busy: false, hasServerProcess: false, hasSyncProcess: false,
            inspect: { _ in XCTFail("same-context proof should be cached"); return false }))
    }

    func testGenerationInvalidationRejectsPendingAndConfirmedProof() throws {
        for confirmed in [false, true] {
            let fixture = try fixture()
            var recovery = OwnedServerRecovery()
            let token = try token(for: fixture, in: &recovery)
            if confirmed {
                XCTAssertTrue(recovery.confirm(
                    token, context: fixture.context, busy: false, hasServerProcess: false, hasSyncProcess: false,
                    inspect: fixture.owner.confirmOwnedProcessTreeExited))
            }
            recovery.invalidate()
            XCTAssertFalse(recovery.confirm(
                token, context: fixture.context, busy: false, hasServerProcess: false, hasSyncProcess: false,
                inspect: { _ in XCTFail("invalidated generation"); return true }))
        }
    }

    func testDifferentRecoveryInstanceCannotUseOldToken() throws {
        let fixture = try fixture()
        var original = OwnedServerRecovery()
        let oldToken = try token(for: fixture, in: &original)
        var replacement = OwnedServerRecovery()
        let newToken = try token(for: fixture, in: &replacement)
        XCTAssertNotEqual(oldToken, newToken)
        XCTAssertFalse(replacement.confirm(
            oldToken, context: fixture.context, busy: false, hasServerProcess: false, hasSyncProcess: false,
            inspect: { _ in XCTFail("different launcher generation"); return true }))
    }

    func testReplacingCaptureDoesNotReuseEarlierProof() throws {
        let first = try fixture()
        let second = try fixture()
        var recovery = OwnedServerRecovery()
        let oldToken = try token(for: first, in: &recovery)
        XCTAssertTrue(recovery.confirm(
            oldToken, context: first.context, busy: false, hasServerProcess: false, hasSyncProcess: false,
            inspect: first.owner.confirmOwnedProcessTreeExited))
        let newToken = try token(for: second, in: &recovery)
        XCTAssertFalse(recovery.confirm(
            oldToken, context: first.context, busy: false, hasServerProcess: false, hasSyncProcess: false,
            inspect: { _ in XCTFail("old ownership capture"); return true }))
        var inspected = false
        XCTAssertTrue(recovery.confirm(
            newToken, context: second.context, busy: false, hasServerProcess: false, hasSyncProcess: false,
            inspect: { process in
                inspected = true
                return second.owner.confirmOwnedProcessTreeExited(process)
            }))
        XCTAssertTrue(inspected)
    }
}
