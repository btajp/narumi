import XCTest

@testable import NarumiMenuBarCore

final class GaiaConnectionRecoveryTests: XCTestCase {
    private let url = GaiaConnectionSettings.defaultURL
    private let replacementKey = "example-replacement-key"

    private func invalidEnvironment() -> GaiaConnectionSettings {
        var settings = GaiaConnectionSettings()
        XCTAssertTrue(settings.beginLoad())
        settings.failed(code: "invalid_argument", message: "Invalid environment settings")
        return settings
    }

    private func arguments(_ request: SetGaiaConnectionRequest?) throws -> [String: Any] {
        let data = try JSONEncoder().encode(XCTUnwrap(request))
        return try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
    }

    func testInvalidEnvironmentLoadEnablesEditorAndDisableButNotTest() {
        let settings = invalidEnvironment()
        XCTAssertTrue(settings.needsEnvironmentRepair)
        XCTAssertFalse(settings.isLoaded)
        XCTAssertTrue(settings.canEdit)
        XCTAssertTrue(settings.canDisable)
        XCTAssertFalse(settings.canSave)
        XCTAssertFalse(settings.canTest)
        XCTAssertEqual(settings.url, "")
        XCTAssertEqual(settings.apiKey, "")
    }

    func testRecoveryRequiresBothExplicitURLAndNewKey() {
        var settings = invalidEnvironment()
        settings.url = url
        XCTAssertFalse(settings.canSave)
        XCTAssertNil(settings.beginSave())
        settings.url = ""
        settings.apiKey = replacementKey
        XCTAssertFalse(settings.canSave)
        XCTAssertNil(settings.beginSave())
        settings.url = " \n"
        XCTAssertFalse(settings.canSave)
        settings.url = url
        XCTAssertTrue(settings.canSave)
    }

    func testRecoveryCannotClearOrRetainAnUnknownKey() throws {
        var settings = invalidEnvironment()
        settings.url = url
        settings.setClearAPIKey(true)
        XCTAssertFalse(settings.clearAPIKey)
        XCTAssertNil(settings.beginSave())
        settings.apiKey = replacementKey
        let args = try arguments(settings.beginSave())
        XCTAssertEqual(args["api_key"] as? String, replacementKey)
        XCTAssertFalse(args["api_key"] is NSNull)
    }

    func testReplacementPayloadIncludesNewURLKeyAndFreshRequestID() throws {
        var settings = invalidEnvironment()
        settings.url = " \(url) \n"
        settings.apiKey = replacementKey
        let args = try arguments(settings.beginSave())
        XCTAssertEqual(Set(args.keys), ["url", "api_key", "request_id"])
        XCTAssertEqual(args["url"] as? String, url)
        XCTAssertEqual(args["api_key"] as? String, replacementKey)
        XCTAssertNotNil(UUID(uuidString: try XCTUnwrap(args["request_id"] as? String)))
        XCTAssertEqual(settings.operation, .saving)
        XCTAssertFalse(settings.canEdit)
        XCTAssertFalse(settings.canSave)
        XCTAssertFalse(settings.canDisable)
        XCTAssertFalse(settings.canTest)
    }

    func testSuccessfulReplacementRestoresNormalLoadedBehavior() throws {
        var settings = invalidEnvironment()
        settings.url = url
        settings.apiKey = replacementKey
        _ = try XCTUnwrap(settings.beginSave())
        settings.saved(GaiaConnection(url: url, hasAPIKey: true, source: .saved))
        XCTAssertFalse(settings.needsEnvironmentRepair)
        XCTAssertTrue(settings.isLoaded)
        XCTAssertTrue(settings.canEdit)
        XCTAssertTrue(settings.canTest)
        XCTAssertFalse(settings.canSave)
        XCTAssertEqual(settings.apiKey, "")
        XCTAssertEqual(settings.connection?.source, .saved)
        settings.setClearAPIKey(true)
        let args = try arguments(settings.beginSave())
        XCTAssertTrue(args["api_key"] is NSNull)
    }

    func testRecoveryDisableSendsNullWithoutAnyKeyOrInvalidEnvironmentValue() throws {
        var settings = invalidEnvironment()
        settings.url = url
        settings.apiKey = replacementKey
        let args = try arguments(settings.beginDisable())
        XCTAssertEqual(Set(args.keys), ["url", "request_id"])
        XCTAssertTrue(args["url"] is NSNull)
        XCTAssertNil(args["api_key"])
        XCTAssertEqual(settings.apiKey, "")
        XCTAssertFalse(settings.canEdit)
        settings.saved(GaiaConnection(url: nil, hasAPIKey: false, source: .saved))
        XCTAssertFalse(settings.needsEnvironmentRepair)
        XCTAssertTrue(settings.isLoaded)
        XCTAssertFalse(settings.canDisable)
        XCTAssertFalse(settings.canTest)
        XCTAssertEqual(settings.connection?.source, .saved)
    }

    func testInvalidLoadNeverDisplaysRawURLOrUnknownCredential() throws {
        var settings = GaiaConnectionSettings()
        settings.beginLoad()
        let rawURL = "http://example-user:example-old-secret@invalid.example/mcp"
        settings.failed(code: "invalid_argument", message: "Rejected \(rawURL), key=example-env-key")
        let error = try XCTUnwrap(settings.errorMessage)
        XCTAssertTrue(error.contains("invalid_argument"))
        XCTAssertTrue(error.contains("新しい URL"))
        XCTAssertFalse(error.contains(rawURL))
        XCTAssertFalse(error.contains("example-old-secret"))
        XCTAssertFalse(error.contains("example-env-key"))
        XCTAssertEqual(settings.url, "")
        XCTAssertEqual(settings.apiKey, "")
    }

    func testUnrecoverableLoadErrorsStayClosedEvenWithReplacementInputs() throws {
        for code in ["internal", "transport", "protocol", "error", "engine_unavailable"] {
            var settings = GaiaConnectionSettings()
            settings.beginLoad()
            settings.failed(code: code, message: "Unreadable value=example-unknown-secret")
            settings.url = url
            settings.apiKey = replacementKey
            XCTAssertFalse(settings.needsEnvironmentRepair, code)
            XCTAssertFalse(settings.canEdit, code)
            XCTAssertFalse(settings.canSave, code)
            XCTAssertFalse(settings.canDisable, code)
            XCTAssertFalse(settings.canTest, code)
            XCTAssertNil(settings.beginSave(), code)
            XCTAssertNil(settings.beginDisable(), code)
            XCTAssertFalse(try XCTUnwrap(settings.errorMessage).contains("example-unknown-secret"), code)
        }
    }

    func testReloadInternalErrorRevokesEarlierRecoveryPermission() {
        var settings = invalidEnvironment()
        settings.url = url
        settings.apiKey = replacementKey
        XCTAssertTrue(settings.beginLoad())
        XCTAssertFalse(settings.needsEnvironmentRepair)
        XCTAssertFalse(settings.canEdit)
        settings.failed(code: "internal", message: "Saved settings are unreadable")
        XCTAssertFalse(settings.needsEnvironmentRepair)
        XCTAssertFalse(settings.canEdit)
        XCTAssertFalse(settings.canSave)
        XCTAssertFalse(settings.canDisable)
        XCTAssertEqual(settings.apiKey, "")
        XCTAssertEqual(settings.url, url)
    }

    func testFailedReloadCannotUsePreviouslyLoadedConnectionForWrites() {
        var settings = GaiaConnectionSettings()
        settings.beginLoad()
        settings.loaded(GaiaConnection(url: url, hasAPIKey: true, source: .saved))
        settings.beginLoad()
        settings.failed(code: "internal", message: "Saved settings are unreadable")
        XCTAssertNil(settings.connection)
        XCTAssertFalse(settings.canEdit)
        XCTAssertFalse(settings.canDisable)
    }

    func testValidReloadExitsRecoveryAndCanStillSaveEnvironmentWithoutKeyReplacement() throws {
        var settings = invalidEnvironment()
        settings.beginLoad()
        settings.loaded(GaiaConnection(url: url, hasAPIKey: true, source: .environment))
        XCTAssertFalse(settings.needsEnvironmentRepair)
        XCTAssertTrue(settings.canTest)
        let args = try arguments(settings.beginSave())
        XCTAssertEqual(args["url"] as? String, url)
        XCTAssertNil(args["api_key"])
    }

    func testInvalidReplacementInputCanBeCorrectedWithoutLosingURL() throws {
        var settings = invalidEnvironment()
        settings.url = url
        settings.apiKey = replacementKey
        _ = try XCTUnwrap(settings.beginSave())
        settings.failed(code: "invalid_argument", message: "Rejected \(replacementKey)")
        XCTAssertTrue(settings.needsEnvironmentRepair)
        XCTAssertTrue(settings.canEdit)
        XCTAssertTrue(settings.canDisable)
        XCTAssertFalse(settings.canSave)
        XCTAssertEqual(settings.url, url)
        XCTAssertEqual(settings.apiKey, "")
        XCTAssertFalse(try XCTUnwrap(settings.errorMessage).contains(replacementKey))
        settings.apiKey = "example-corrected-key"
        XCTAssertTrue(settings.canSave)
    }

    func testFailedRecoveryWriteRequiresReloadForNonValidationErrors() throws {
        for code in ["internal", "transport"] {
            var settings = invalidEnvironment()
            settings.url = url
            settings.apiKey = replacementKey
            _ = try XCTUnwrap(settings.beginSave())
            settings.failed(code: code, message: "Unable to save settings")
            XCTAssertFalse(settings.needsEnvironmentRepair, code)
            XCTAssertFalse(settings.canEdit, code)
            XCTAssertFalse(settings.canSave, code)
            XCTAssertFalse(settings.canDisable, code)
            XCTAssertEqual(settings.url, url)
            XCTAssertEqual(settings.apiKey, "")
            XCTAssertTrue(settings.beginLoad())
        }
    }

    func testFailedRecoveryDisableDoesNotLeaveAWriteBypass() throws {
        var settings = invalidEnvironment()
        _ = try XCTUnwrap(settings.beginDisable())
        settings.failed(code: "internal", message: "Saved settings are unreadable")
        XCTAssertFalse(settings.canEdit)
        XCTAssertFalse(settings.canDisable)
        XCTAssertNil(settings.beginDisable())
    }

    func testWriteValidationFailureDoesNotGrantEnvironmentRecovery() {
        var settings = GaiaConnectionSettings()
        settings.beginLoad()
        settings.loaded(GaiaConnection(url: url, hasAPIKey: true, source: .saved))
        settings.apiKey = replacementKey
        _ = settings.beginSave()
        settings.failed(code: "invalid_argument", message: "Invalid replacement key")
        XCTAssertFalse(settings.needsEnvironmentRepair)
        XCTAssertTrue(settings.isLoaded)
        XCTAssertTrue(settings.canEdit)
    }

    func testDismissRevokesRecoveryAndClearsSecretInput() {
        var settings = invalidEnvironment()
        settings.url = url
        settings.apiKey = replacementKey
        settings.dismiss()
        XCTAssertFalse(settings.needsEnvironmentRepair)
        XCTAssertFalse(settings.canEdit)
        XCTAssertFalse(settings.canDisable)
        XCTAssertEqual(settings.url, "")
        XCTAssertEqual(settings.apiKey, "")
        XCTAssertNil(settings.errorMessage)
    }
}
