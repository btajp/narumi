import XCTest

@testable import NarumiMenuBarCore

final class KeychainHelperProcessExecutorTests: XCTestCase {
    /// Synthetic shell fixtures never call the real helper or Security framework.
    private func withHelper(_ body: String, run: (URL) throws -> Void) throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: false)
        defer { try? FileManager.default.removeItem(at: root) }
        let helper = root.appendingPathComponent("narumi-keychain")
        try Data(("#!/bin/sh\n" + body).utf8).write(to: helper)
        try FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: helper.path)
        try run(helper)
    }

    func testPipeInputIsClosedAndArgumentsAndInheritedEnvironmentAreAbsent() throws {
        try withHelper("""
            [ "$#" -eq 0 ] || exit 31
            [ -z "${HOME+x}" ] || exit 32
            IFS= read -r request
            printf '%s' "$request"
            """) { helper in
                let input = Data(#"{"operation":"get","account":"fixture"}"#.utf8)
                let result = try KeychainHelperProcessExecutor().run(executable: helper, input: input)
                XCTAssertEqual(result.exitStatus, 0)
                XCTAssertEqual(result.output, input)
            }
    }

    func testReadsRepliesLargerThanPipeCapacityWithoutWaitingForChildExitFirst() throws {
        try withHelper("""
            IFS= read -r request
            i=0
            while [ "$i" -lt 5000 ]; do
              printf '0123456789'
              i=$((i + 1))
            done
            """) { helper in
                let result = try KeychainHelperProcessExecutor().run(executable: helper, input: Data("{}".utf8))
                XCTAssertEqual(result.exitStatus, 0)
                XCTAssertEqual(result.output.count, 50_000)
            }
    }

    func testOversizedOutputFailsWithAFixedError() throws {
        try withHelper("""
            IFS= read -r request
            while :; do printf '0123456789'; done
            """) { helper in
                XCTAssertThrowsError(try KeychainHelperProcessExecutor().run(executable: helper, input: Data("{}".utf8))) {
                    XCTAssertEqual($0 as? KeychainSecretError, .keychainUnavailable)
                }
            }
    }

    func testStalledHelperIsTerminatedWhetherOrNotItClosesStdout() throws {
        for closeStdout in [false, true] {
            try withHelper((closeStdout ? "exec 1>&-\n" : "") + "exec /bin/sleep 10\n") { helper in
                let executor = KeychainHelperProcessExecutor(timeoutNanoseconds: 100_000_000)
                let start = Date()
                XCTAssertThrowsError(try executor.run(executable: helper, input: Data("{}".utf8))) {
                    XCTAssertEqual($0 as? KeychainSecretError, .keychainUnavailable)
                }
                XCTAssertLessThan(Date().timeIntervalSince(start), 2)
            }
        }
    }

    func testEarlyExitCannotKillTheClientWithSIGPIPE() throws {
        try withHelper("exit 7\n") { helper in
            let reader = KeychainHelperSecretReader(helperURL: helper)
            XCTAssertThrowsError(try reader.get(account: "fixture")) {
                XCTAssertEqual($0 as? KeychainSecretError, .keychainUnavailable)
            }
        }
    }

    func testMissingExecutableFailsWithoutAFileFallback() {
        let helper = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        XCTAssertThrowsError(try KeychainHelperProcessExecutor().run(executable: helper, input: Data("{}".utf8))) {
            XCTAssertEqual($0 as? KeychainSecretError, .keychainUnavailable)
        }
        XCTAssertFalse(FileManager.default.fileExists(atPath: helper.path))
    }
}
