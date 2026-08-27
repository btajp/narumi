import XCTest

@testable import NarumiMenuBarCore

final class GaiaConnectionModelsTests: XCTestCase {
    private let url = "http://127.0.0.1:4111/mcp"
    private let requestID = "1a5d66a0-bb27-4cb9-87c1-1cd8f8068f05"

    private func arguments<T: Encodable>(_ request: T) throws -> [String: Any] {
        try XCTUnwrap(JSONSerialization.jsonObject(with: JSONEncoder().encode(request)) as? [String: Any])
    }

    func testUnchangedKeyIsOmittedRatherThanNullOrEmpty() throws {
        let request = SetGaiaConnectionRequest(url: url, requestID: requestID)
        let args = try arguments(request)
        XCTAssertEqual(Set(args.keys), ["url", "request_id"])
        XCTAssertEqual(args["url"] as? String, url)
        XCTAssertEqual(args["request_id"] as? String, requestID)
        XCTAssertNil(args["api_key"])
    }

    func testExplicitKeyClearEncodesJSONNull() throws {
        let request = SetGaiaConnectionRequest(url: url, apiKey: .clear, requestID: requestID)
        let args = try arguments(request)
        XCTAssertTrue(args["api_key"] is NSNull)
        XCTAssertEqual(args["url"] as? String, url)
    }

    func testReplacementKeyIsEncodedVerbatim() throws {
        let request = SetGaiaConnectionRequest(
            url: url, apiKey: .replace("example-not-a-real-key"), requestID: requestID)
        let args = try arguments(request)
        XCTAssertEqual(args["api_key"] as? String, "example-not-a-real-key")
        XCTAssertEqual(Set(args.keys), ["url", "api_key", "request_id"])
    }

    func testDisableEncodesURLNullAndDoesNotIncludeAKey() throws {
        let args = try arguments(SetGaiaConnectionRequest(url: nil, requestID: requestID))
        XCTAssertTrue(args["url"] is NSNull)
        XCTAssertNil(args["api_key"])
        XCTAssertEqual(args["request_id"] as? String, requestID)
    }

    func testEveryWriteHasAFreshUUID() {
        let first = SetGaiaConnectionRequest(url: url)
        let second = SetGaiaConnectionRequest(url: url)
        XCTAssertNotEqual(first.requestID, second.requestID)
        XCTAssertNotNil(UUID(uuidString: first.requestID))
        XCTAssertNotNil(UUID(uuidString: second.requestID))
    }

    func testConnectionTestContainsOnlyDefaultTimeout() throws {
        let args = try arguments(TestGaiaConnectionRequest())
        XCTAssertEqual(Set(args.keys), ["timeout_seconds"])
        XCTAssertEqual(args["timeout_seconds"] as? Double, 5)
    }

    func testConnectionTestCanOverrideTimeout() throws {
        let args = try arguments(TestGaiaConnectionRequest(timeoutSeconds: 3))
        XCTAssertEqual(args["timeout_seconds"] as? Double, 3)
    }

    func testPublicConnectionContainsOnlyCredentialFreeFields() throws {
        let connection = GaiaConnection(url: url, hasAPIKey: true, source: .environment)
        let args = try arguments(connection)
        XCTAssertEqual(Set(args.keys), ["url", "has_api_key", "source"])
        XCTAssertEqual(args["has_api_key"] as? Bool, true)
        XCTAssertEqual(args["source"] as? String, "environment")
    }

    func testUnknownSettingsSourceDoesNotDecode() {
        let data = Data(#"{"url":null,"has_api_key":false,"source":"unknown"}"#.utf8)
        XCTAssertThrowsError(try JSONDecoder().decode(GaiaConnection.self, from: data))
    }

    func testDisabledConnectionRetainsRequiredNullURLWhenEncoded() throws {
        let args = try arguments(GaiaConnection(url: nil, hasAPIKey: false, source: .saved))
        XCTAssertTrue(args["url"] is NSNull)
        XCTAssertEqual(Set(args.keys), ["url", "has_api_key", "source"])
    }

    func testMissingURLDoesNotDecodeAsDisabled() {
        let data = Data(#"{"has_api_key":false,"source":"saved"}"#.utf8)
        XCTAssertThrowsError(try JSONDecoder().decode(GaiaConnection.self, from: data))
    }

    func testHumanIdentityAndNullDefaultScopeDecode() throws {
        let data = Data("""
            {"connected":true,"name":"gaia_library","version":"0.1.0","contract_version":"1.0.0",
             "client":{"name":"test-user","role":"human","default_scope":null}}
            """.utf8)
        let result = try JSONDecoder().decode(GaiaConnectionTestResult.self, from: data)
        XCTAssertEqual(result.client.name, "test-user")
        XCTAssertEqual(result.client.role, .human)
        XCTAssertNil(result.client.defaultScope)
    }

    func testDisconnectedResponseCannotLookSuccessful() {
        let data = Data("""
            {"connected":false,"name":"gaia_library","version":"0.1.0","contract_version":"1.0.0",
             "client":{"name":"narumi","role":"agent","default_scope":null}}
            """.utf8)
        XCTAssertThrowsError(try JSONDecoder().decode(GaiaConnectionTestResult.self, from: data))
    }

    func testMissingDefaultScopeDoesNotDecodeAsUnscoped() {
        let data = Data("""
            {"connected":true,"name":"gaia_library","version":"0.1.0","contract_version":"1.0.0",
             "client":{"name":"narumi","role":"agent"}}
            """.utf8)
        XCTAssertThrowsError(try JSONDecoder().decode(GaiaConnectionTestResult.self, from: data))
    }
}
