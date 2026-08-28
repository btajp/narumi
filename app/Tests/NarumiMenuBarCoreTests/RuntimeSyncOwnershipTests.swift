import XCTest

@testable import NarumiMenuBarCore

final class RuntimeSyncOwnershipTests: XCTestCase {
    private let child = RuntimeSyncOwnership.Identity(
        pid: 1234, startedSeconds: 100, startedMicroseconds: 20, processGroup: 1234)

    private func fixture(
        childRecorded: Bool = true, boot: String = "same-boot", current: RuntimeSyncOwnership.Identity? = nil,
        groupAlive: Bool = false
    ) throws -> (RuntimeSyncOwnership, URL) {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent("narumi-sync-owner-\(UUID().uuidString)")
        let paths = RuntimePaths(dataRoot: root)
        try FileManager.default.createDirectory(at: paths.root, withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: root) }
        let owner = RuntimeSyncOwnership(paths: paths, inspection: .init(
            bootSession: { boot }, identity: { _ in current }, groupExists: { _ in groupAlive }))
        let record = RuntimeSyncOwnership.Record(
            token: "fixture", bootSession: "same-boot", app: child, child: childRecorded ? child : nil)
        try JSONEncoder().encode(record).write(to: owner.recordURL)
        return (owner, root)
    }

    func testMatchingPIDAndStartTimeBlocksEvenWithoutListeningPort() throws {
        let (owner, _) = try fixture(current: child)
        XCTAssertThrowsError(try owner.requireIdle())
        XCTAssertTrue(FileManager.default.fileExists(atPath: owner.recordURL.path))
    }

    func testPIDReuseIsNotTreatedAsOriginalUV() throws {
        var reused = child
        reused.startedMicroseconds += 1
        let (owner, _) = try fixture(current: reused)
        XCTAssertNoThrow(try owner.requireIdle())
        XCTAssertFalse(FileManager.default.fileExists(atPath: owner.recordURL.path))
    }

    func testExitedUVWithRemainingProcessGroupStaysBlocked() throws {
        let (owner, _) = try fixture(groupAlive: true)
        XCTAssertThrowsError(try owner.requireIdle())
        XCTAssertTrue(FileManager.default.fileExists(atPath: owner.recordURL.path))
    }

    func testUnknownSpawnIntentFailsClosedForSameBoot() throws {
        let (owner, _) = try fixture(childRecorded: false)
        XCTAssertThrowsError(try owner.requireIdle())
        XCTAssertTrue(FileManager.default.fileExists(atPath: owner.recordURL.path))
    }

    func testNewBootMakesEvenUnknownOldIntentSafeToClear() throws {
        let (owner, _) = try fixture(childRecorded: false, boot: "next-boot")
        XCTAssertNoThrow(try owner.requireIdle())
        XCTAssertFalse(FileManager.default.fileExists(atPath: owner.recordURL.path))
    }

    func testCorruptRecordIsNotDeleted() throws {
        let (owner, _) = try fixture()
        try Data("broken record".utf8).write(to: owner.recordURL)
        XCTAssertThrowsError(try owner.requireIdle())
        XCTAssertEqual(try Data(contentsOf: owner.recordURL), Data("broken record".utf8))
    }

    func testUnknownProcessIdentityDoesNotPermitCleanup() throws {
        let (owner, root) = try fixture()
        struct Unknown: Error {}
        let unknown = RuntimeSyncOwnership(paths: RuntimePaths(dataRoot: root), inspection: .init(
            bootSession: { "same-boot" }, identity: { _ in throw Unknown() }, groupExists: { _ in false }))
        XCTAssertThrowsError(try unknown.requireIdle())
        XCTAssertTrue(FileManager.default.fileExists(atPath: owner.recordURL.path))
    }
}
