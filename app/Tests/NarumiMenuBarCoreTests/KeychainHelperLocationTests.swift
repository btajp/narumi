import Darwin
import Foundation
import XCTest

@testable import NarumiMenuBarCore

final class KeychainHelperLocationTests: XCTestCase {
    func testKnownBundleHelperValidatesWithoutExecutingIt() throws {
        let fixture = try makeFixture()
        defer { try? FileManager.default.removeItem(at: fixture.root) }
        let location = KeychainHelperLocation(trustedRoot: fixture.root, candidate: fixture.helper)
        XCTAssertEqual(try location.validatedExecutable(), fixture.helper.resolvingSymlinksInPath())
    }

    func testBuildDirectoryLinkMayResolveOnlyInsideKnownBuildRoot() throws {
        let root = try temporaryRoot()
        defer { try? FileManager.default.removeItem(at: root) }
        let build = root.appendingPathComponent("app/.build")
        let actual = build.appendingPathComponent("arm64-apple-macosx/debug")
        try FileManager.default.createDirectory(at: actual, withIntermediateDirectories: true,
            attributes: [.posixPermissions: 0o700])
        let executable = actual.appendingPathComponent("narumi-keychain")
        try makeExecutable(executable)
        let alias = build.appendingPathComponent("debug")
        try FileManager.default.createSymbolicLink(at: alias, withDestinationURL: actual)
        let location = KeychainHelperLocation(
            trustedRoot: root, candidate: alias.appendingPathComponent("narumi-keychain"), buildRoot: build)
        XCTAssertEqual(try location.validatedExecutable(), executable.resolvingSymlinksInPath())
        try FileManager.default.removeItem(at: alias)
        let outside = root.appendingPathComponent("outside")
        try FileManager.default.createDirectory(at: outside, withIntermediateDirectories: false)
        try makeExecutable(outside.appendingPathComponent("narumi-keychain"))
        try FileManager.default.createSymbolicLink(at: alias, withDestinationURL: outside)
        XCTAssertThrowsError(try location.validatedExecutable())
    }

    func testExecutableSymlinkAndNonRegularFileAreRejectedBeforeExecution() throws {
        for fifo in [false, true] {
            let fixture = try makeFixture()
            defer { try? FileManager.default.removeItem(at: fixture.root) }
            let original = fixture.helper
            try FileManager.default.removeItem(at: original)
            if fifo {
                XCTAssertEqual(mkfifo(original.path, 0o700), 0)
            } else {
                let other = fixture.root.appendingPathComponent("other-helper")
                try makeExecutable(other)
                try FileManager.default.createSymbolicLink(at: original, withDestinationURL: other)
            }
            let location = KeychainHelperLocation(trustedRoot: fixture.root, candidate: original)
            XCTAssertThrowsError(try location.validatedExecutable()) {
                XCTAssertEqual($0 as? MCPConnectionError, .credentialUnavailable)
            }
        }
    }

    func testUnsafeOwnerPermissionsAndMissingExecuteBitAreRejected() throws {
        for (part, mode) in [("helper", 0o777), ("helper", 0o644), ("root", 0o775), ("parent", 0o777)] {
            let fixture = try makeFixture()
            defer { try? FileManager.default.removeItem(at: fixture.root) }
            let path = part == "helper" ? fixture.helper : (part == "root" ? fixture.root : fixture.helper.deletingLastPathComponent())
            XCTAssertEqual(chmod(path.path, mode_t(mode)), 0)
            let location = KeychainHelperLocation(trustedRoot: fixture.root, candidate: fixture.helper)
            XCTAssertThrowsError(try location.validatedExecutable()) {
                XCTAssertEqual($0 as? MCPConnectionError, .credentialUnavailable)
                XCTAssertFalse($0.localizedDescription.contains(path.path))
            }
        }
    }

    func testBundleDirectorySymlinkIsNotAValidHelperLocation() throws {
        let fixture = try makeFixture()
        defer { try? FileManager.default.removeItem(at: fixture.root) }
        let parent = fixture.helper.deletingLastPathComponent()
        let moved = fixture.root.appendingPathComponent("moved")
        try FileManager.default.moveItem(at: parent, to: moved)
        try FileManager.default.createSymbolicLink(at: parent, withDestinationURL: moved)
        XCTAssertThrowsError(try KeychainHelperLocation(trustedRoot: fixture.root, candidate: fixture.helper).validatedExecutable())
    }

    private func makeFixture() throws -> (root: URL, helper: URL) {
        let root = try temporaryRoot()
        let directory = root.appendingPathComponent("dist/narumi.app/Contents/MacOS")
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true,
            attributes: [.posixPermissions: 0o700])
        let helper = directory.appendingPathComponent("narumi-keychain")
        try makeExecutable(helper)
        return (root, helper)
    }

    private func temporaryRoot() throws -> URL {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent("narumi-helper-location-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true,
            attributes: [.posixPermissions: 0o700])
        return root
    }

    private func makeExecutable(_ url: URL) throws {
        // A marker script is sufficient for metadata validation and is never executed.
        try Data("#!/bin/sh\nexit 0\n".utf8).write(to: url)
        guard chmod(url.path, 0o755) == 0 else { throw CocoaError(.fileWriteNoPermission) }
    }
}
