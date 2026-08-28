import XCTest

@testable import NarumiMenuBarCore

final class KeychainHelperSecretReaderTests: XCTestCase {
    private let helperURL = URL(fileURLWithPath: "/Applications/narumi.app/Contents/MacOS/narumi-keychain")

    func testReaderRequestsOnlyAnExistingItemThroughTheExplicitHelper() throws {
        let executor = KeychainTestExecutor(json: #"{"ok":true,"value":"fixture-token"}"#)
        let reader = KeychainHelperSecretReader(helperURL: helperURL, executor: executor)
        XCTAssertEqual(try reader.get(account: "transport:fixture-root:fixture-instance"), "fixture-token")
        let call = try XCTUnwrap(executor.calls.first)
        XCTAssertEqual(call.executable, helperURL)
        XCTAssertEqual(try JSONSerialization.jsonObject(with: call.input) as? [String: String], [
            "operation": "get", "account": "transport:fixture-root:fixture-instance",
        ])
        XCTAssertEqual(executor.calls.count, 1)
    }

    func testMissingItemRemainsNilAndIsNeverCreated() throws {
        let executor = KeychainTestExecutor(json: #"{"ok":true,"value":null}"#)
        let reader = KeychainHelperSecretReader(helperURL: helperURL, executor: executor)
        XCTAssertNil(try reader.get(account: "fixture"))
        XCTAssertEqual(executor.calls.count, 1)
    }

    func testNonzeroExitRejectsEvenASuccessShapedReply() {
        let executor = KeychainTestExecutor(json: #"{"ok":true,"value":"fixture-token"}"#, exitStatus: 1)
        let reader = KeychainHelperSecretReader(helperURL: helperURL, executor: executor)
        XCTAssertThrowsError(try reader.get(account: "fixture")) {
            XCTAssertEqual($0 as? KeychainSecretError, .keychainUnavailable)
        }
    }

    func testInvalidOrAmbiguousRepliesAreNeverAccepted() {
        for json in [
            #"{}"#, #"{"ok":true}"#, #"{"ok":1,"value":"fixture-token"}"#,
            #"{"ok":true,"ok":false,"value":"fixture-token"}"#,
            #"{"ok":true,"value":"fixture-token","value":"other-token"}"#,
            #"{"ok":false,"error":"fixture-sensitive"}"#,
            #"{"ok":true,"value":"fixture-token","extra":null}"#,
            #"{"ok":true,"value":["fixture-token"]}"#,
            #"{"ok":true,"value":12}"#,
            #"{"ok":true,"value":null}{}"#,
            #"{"ok":true,"value":""}"#,
            #"{"ok":true,"value":"fixture\r\ntoken"}"#,
        ] {
            let reader = KeychainHelperSecretReader(
                helperURL: helperURL, executor: KeychainTestExecutor(json: json))
            XCTAssertThrowsError(try reader.get(account: "fixture")) {
                XCTAssertTrue($0 is KeychainSecretError)
                XCTAssertFalse(String(describing: $0).contains("fixture-sensitive"))
            }
        }
    }

    func testOversizedRepliesAndSecretsAreRejected() {
        for count in [KeychainSecretStore.maximumValueBytes + 1, KeychainHelperProtocol.maximumResponseBytes] {
            let json = "{\"ok\":true,\"value\":\"" + String(repeating: "a", count: count) + "\"}"
            let reader = KeychainHelperSecretReader(
                helperURL: helperURL, executor: KeychainTestExecutor(json: json))
            XCTAssertThrowsError(try reader.get(account: "fixture"))
        }
    }

    func testInvalidAccountAndNonFileHelperNeverInvokeTheExecutor() {
        let executor = KeychainTestExecutor(json: #"{"ok":true,"value":null}"#)
        let reader = KeychainHelperSecretReader(helperURL: helperURL, executor: executor)
        XCTAssertThrowsError(try reader.get(account: "fixture/bad")) {
            XCTAssertEqual($0 as? KeychainSecretError, .invalidAccount)
        }
        let remote = KeychainHelperSecretReader(helperURL: URL(string: "https://example.invalid/helper")!, executor: executor)
        XCTAssertThrowsError(try remote.get(account: "fixture")) {
            XCTAssertEqual($0 as? KeychainSecretError, .keychainUnavailable)
        }
        XCTAssertTrue(executor.calls.isEmpty)
    }

    func testExecutorExceptionsCannotLeakTheirDetails() {
        let executor = KeychainTestExecutor(json: "")
        executor.fail = true
        let reader = KeychainHelperSecretReader(helperURL: helperURL, executor: executor)
        XCTAssertThrowsError(try reader.get(account: "fixture")) {
            XCTAssertEqual($0 as? KeychainSecretError, .keychainUnavailable)
            XCTAssertFalse(String(describing: $0).contains("fixture-sensitive"))
        }
    }
}

private final class KeychainTestExecutor: KeychainHelperExecuting, @unchecked Sendable {
    struct Call {
        let executable: URL
        let input: Data
    }
    struct PrivateFailure: Error, CustomStringConvertible {
        let description = "fixture-sensitive process detail"
    }
    var calls: [Call] = []
    var fail = false
    let result: KeychainHelperResult

    init(json: String, exitStatus: Int32 = 0) {
        self.result = KeychainHelperResult(output: Data(json.utf8), exitStatus: exitStatus)
    }

    func run(executable: URL, input: Data) throws -> KeychainHelperResult {
        calls.append(Call(executable: executable, input: input))
        if fail { throw PrivateFailure() }
        return result
    }
}
