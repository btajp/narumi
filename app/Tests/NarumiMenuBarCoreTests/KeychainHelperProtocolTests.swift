import XCTest

@testable import NarumiMenuBarCore

final class KeychainHelperProtocolTests: XCTestCase {
    func testJSONRequestsRoundTripThroughMemoryBackendAndNeverEchoMutations() throws {
        let backend = KeychainTestBackend()
        let store = KeychainSecretStore(backend: backend)
        func call(_ fields: [String: Any]) throws -> [String: Any] {
            let result = KeychainHelperProtocol.handle(
                try JSONSerialization.data(withJSONObject: fields), store: store)
            XCTAssertEqual(result.exitStatus, 0)
            return try XCTUnwrap(JSONSerialization.jsonObject(with: result.output) as? [String: Any])
        }
        let account = "transport:fixture-root:fixture-instance"
        let initial = try call(["operation": "get", "account": account])
        XCTAssertTrue(initial["value"] is NSNull)
        for value in ["fixture-first", "fixture-second"] {
            let updated = try call(["operation": "set", "account": account, "value": value])
            XCTAssertEqual(Set(updated.keys), ["ok"])
            let fetched = try call(["operation": "get", "account": account])
            XCTAssertEqual(fetched["value"] as? String, value)
        }
        XCTAssertEqual(Set(try call(["operation": "delete", "account": account]).keys), ["ok"])
        XCTAssertTrue(try call(["operation": "get", "account": account])["value"] is NSNull)
    }

    func testMalformedOrAmbiguousRequestsFailBeforeTouchingBackend() {
        let invalid = [
            "", "[]", "null", "{}", "{", "{} {}",
            #"{"operation":"get","account":"fixture","extra":"fixture-secret"}"#,
            #"{"operation":"get","account":"fixture","value":"fixture-secret"}"#,
            #"{"operation":"delete","account":"fixture","value":null}"#,
            #"{"operation":"set","account":"fixture"}"#,
            #"{"operation":"set","account":"fixture","value":null}"#,
            #"{"operation":"set","account":"fixture","value":true}"#,
            #"{"operation":"get","account":12}"#,
            #"{"operation":true,"account":"fixture"}"#,
            #"{"operation":"get","account":{"service":"other"}}"#,
            #"{"operation":"get","account":"fixture","service":"other"}"#,
            #"{"operation":"list","account":"fixture"}"#,
            #"{"operation":"get","operation":"delete","account":"fixture"}"#,
            #"{"operation":"get","account":"fixture","accoun\u0074":"other"}"#,
            #"{"operation":"get","account":"fixture",}"#,
            #"{"operation":"get","account":"fixture"} trailing"#,
            #"{"operation":"get","account":"fixture""#,
            #"{"operation":"get","account":"\ud800"}"#,
            #"{"operation":"get","account":"a/b"}"#,
            #"{"operation":"get","account":"日本語"}"#,
            #"{"operation":"set","account":"fixture","value":"a\u0000b"}"#,
            #"{"operation":"set","account":"fixture","value":"a\nb"}"#,
            #"{"operation":"set","account":"fixture","value":""}"#,
        ]
        for json in invalid {
            let backend = KeychainTestBackend()
            let result = KeychainHelperProtocol.handle(Data(json.utf8), store: KeychainSecretStore(backend: backend))
            XCTAssertNotEqual(result.exitStatus, 0, json)
            XCTAssertEqual(String(data: result.output, encoding: .utf8), "{\"error\":\"invalid_request\",\"ok\":false}\n")
            XCTAssertTrue(backend.calls.isEmpty)
        }
    }

    func testEscapedStringFieldsAreParsedWithoutChangingTheirMeaning() throws {
        let backend = KeychainTestBackend()
        let store = KeychainSecretStore(backend: backend)
        let json = #" { "oper\u0061tion": "set", "account": "fixture", "value": "test\"value\\fixture" } "#
        XCTAssertEqual(KeychainHelperProtocol.handle(Data(json.utf8), store: store).exitStatus, 0)
        XCTAssertEqual(try store.get(account: "fixture"), "test\"value\\fixture")
    }

    func testArgumentsAndOversizedInputAreRejectedBeforeDecode() {
        let backend = KeychainTestBackend()
        let store = KeychainSecretStore(backend: backend)
        let valid = Data(#"{"operation":"get","account":"fixture"}"#.utf8)
        for count in [0, 2, 3] {
            XCTAssertNotEqual(KeychainHelperProtocol.handle(valid, argumentCount: count, store: store).exitStatus, 0)
        }
        let huge = Data(repeating: 32, count: KeychainHelperProtocol.maximumRequestBytes + 1)
        XCTAssertNotEqual(KeychainHelperProtocol.handle(huge, store: store).exitStatus, 0)
        XCTAssertNotEqual(KeychainHelperProtocol.handle(Data([0xff]), store: store).exitStatus, 0)
        XCTAssertTrue(backend.calls.isEmpty)
    }

    func testBackendErrorsUseFixedCodesWithoutSecretDetails() throws {
        let request = Data(#"{"operation":"set","account":"fixture","value":"fixture-sensitive"}"#.utf8)
        let result = KeychainHelperProtocol.handle(request, store: FailingSecretStore())
        XCTAssertNotEqual(result.exitStatus, 0)
        let fields = try XCTUnwrap(JSONSerialization.jsonObject(with: result.output) as? [String: Any])
        XCTAssertEqual(fields["error"] as? String, "keychain_unavailable")
        XCTAssertEqual(Set(fields.keys), ["ok", "error"])
        XCTAssertFalse(String(decoding: result.output, as: UTF8.self).contains("fixture-sensitive"))
    }
}

private struct FailingSecretStore: KeychainSecretStoring {
    struct PrivateFailure: Error, CustomStringConvertible {
        let description = "fixture-sensitive backend detail"
    }
    func get(account: String) throws -> String? { throw PrivateFailure() }
    func set(value: String, account: String) throws { throw PrivateFailure() }
    func delete(account: String) throws { throw PrivateFailure() }
}
