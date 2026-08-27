import XCTest

@testable import NarumiMenuBarCore

final class GaiaConnectionSettingsTests: XCTestCase {
    private let url = GaiaConnectionSettings.defaultURL
    private let replacementURL = "http://127.0.0.1:4222/mcp"

    private func loaded(
        source: GaiaConnection.Source = .saved, hasAPIKey: Bool = true
    ) -> GaiaConnectionSettings {
        var settings = GaiaConnectionSettings()
        XCTAssertTrue(settings.beginLoad())
        settings.loaded(GaiaConnection(url: url, hasAPIKey: hasAPIKey, source: source))
        return settings
    }

    private func arguments(_ request: SetGaiaConnectionRequest?) throws -> [String: Any] {
        let data = try JSONEncoder().encode(XCTUnwrap(request))
        return try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
    }

    private func testResult() throws -> GaiaConnectionTestResult {
        let data = Data("""
            {"connected":true,"name":"gaia_library","version":"0.1.0","contract_version":"1.0.0",
             "client":{"name":"narumi","role":"agent","default_scope":"cloudnative"}}
            """.utf8)
        return try JSONDecoder().decode(GaiaConnectionTestResult.self, from: data)
    }

    func testInitiallyNoActionsBeforeSettingsLoad() {
        let settings = GaiaConnectionSettings()
        XCTAssertFalse(settings.isLoaded)
        XCTAssertFalse(settings.canSave)
        XCTAssertFalse(settings.canTest)
        XCTAssertFalse(settings.canDisable)
        XCTAssertEqual(settings.url, "")
        XCTAssertEqual(GaiaConnectionSettings.defaultURL, "http://127.0.0.1:4111/mcp")
    }

    func testUnconfiguredGaiaIsOptionalAndNotAutoEnabled() {
        var settings = GaiaConnectionSettings()
        settings.beginLoad()
        settings.loaded(GaiaConnection(url: nil, hasAPIKey: false, source: .unconfigured))
        XCTAssertTrue(settings.isLoaded)
        XCTAssertFalse(settings.isBusy)
        XCTAssertFalse(settings.canSave)
        XCTAssertFalse(settings.canTest)
        XCTAssertNil(settings.errorMessage)
        XCTAssertEqual(settings.url, "")
        settings.url = url
        XCTAssertTrue(settings.canSave)
    }

    func testReadingNeverPopulatesSecretInput() {
        let settings = loaded()
        XCTAssertTrue(settings.connection?.hasAPIKey ?? false)
        XCTAssertEqual(settings.apiKey, "")
        XCTAssertEqual(settings.url, url)
        XCTAssertFalse(settings.hasUnsavedChanges)
        XCTAssertTrue(settings.canTest)
    }

    func testEnvironmentConfigurationCanBeSavedWithoutReplacingItsKey() throws {
        var settings = loaded(source: .environment)
        XCTAssertTrue(settings.canSave)
        XCTAssertTrue(settings.canTest)
        let args = try arguments(settings.beginSave())
        XCTAssertEqual(args["url"] as? String, url)
        XCTAssertNil(args["api_key"])
        settings.saved(GaiaConnection(url: url, hasAPIKey: true, source: .saved))
        XCTAssertEqual(settings.connection?.source, .saved)
        XCTAssertFalse(settings.canSave)
    }

    func testBlankKeyWithChangedURLIsOmittedAndShowsURLChangeWarning() throws {
        var settings = loaded()
        settings.url = replacementURL
        XCTAssertTrue(settings.changesExistingURL)
        XCTAssertFalse(settings.canTest)
        let args = try arguments(settings.beginSave())
        XCTAssertEqual(args["url"] as? String, replacementURL)
        XCTAssertNil(args["api_key"])
    }

    func testNewKeyWithChangedURLIsSentTogetherThenClearedOnSuccess() throws {
        var settings = loaded()
        settings.url = replacementURL
        settings.apiKey = "example-new-key"
        let args = try arguments(settings.beginSave())
        XCTAssertEqual(args["url"] as? String, replacementURL)
        XCTAssertEqual(args["api_key"] as? String, "example-new-key")
        settings.saved(GaiaConnection(url: replacementURL, hasAPIKey: true, source: .saved))
        XCTAssertEqual(settings.apiKey, "")
        XCTAssertEqual(settings.url, replacementURL)
        XCTAssertFalse(settings.hasUnsavedChanges)
        XCTAssertTrue(settings.canTest)
        XCTAssertNotNil(settings.notice)
    }

    func testExplicitKeyClearRemovesInputAndEncodesNull() throws {
        var settings = loaded()
        settings.apiKey = "example-new-key"
        settings.setClearAPIKey(true)
        XCTAssertEqual(settings.apiKey, "")
        XCTAssertTrue(settings.hasUnsavedChanges)
        XCTAssertFalse(settings.canTest)
        let args = try arguments(settings.beginSave())
        XCTAssertTrue(args["api_key"] is NSNull)
        settings.saved(GaiaConnection(url: url, hasAPIKey: false, source: .saved))
        XCTAssertFalse(settings.clearAPIKey)
        XCTAssertFalse(settings.connection?.hasAPIKey ?? true)
    }

    func testOuterURLWhitespaceIsNormalizedWithoutChangingEffectiveURL() {
        var settings = loaded()
        settings.url = " \n\(url) \n"
        XCTAssertEqual(settings.normalizedURL, url)
        XCTAssertFalse(settings.hasUnsavedChanges)
        XCTAssertFalse(settings.changesExistingURL)
        XCTAssertTrue(settings.canTest)
    }

    func testBlankURLCannotAccidentallyDisableGaiaThroughSave() {
        var settings = loaded()
        settings.url = " \n"
        XCTAssertFalse(settings.canSave)
        XCTAssertNil(settings.beginSave())
        XCTAssertTrue(settings.canDisable)
    }

    func testDisableAlwaysSendsNullURLAndNeverTheDraftKey() throws {
        var settings = loaded(source: .environment)
        settings.url = replacementURL
        settings.apiKey = "example-unsaved-key"
        let args = try arguments(settings.beginDisable())
        XCTAssertTrue(args["url"] is NSNull)
        XCTAssertNil(args["api_key"])
        XCTAssertEqual(settings.apiKey, "")
        settings.saved(GaiaConnection(url: nil, hasAPIKey: false, source: .saved))
        XCTAssertNil(settings.connection?.url)
        XCTAssertEqual(settings.connection?.source, .saved)
        XCTAssertFalse(settings.canDisable)
        XCTAssertFalse(settings.canTest)
        XCTAssertEqual(settings.url, "")
    }

    func testFailedSaveRetainsURLAndHidesThenClearsSecret() throws {
        var settings = loaded()
        settings.url = replacementURL
        settings.apiKey = "example-secret-key"
        _ = try XCTUnwrap(settings.beginSave())
        settings.failed(code: "invalid_argument", message: "Rejected example-secret-key")
        XCTAssertEqual(settings.url, replacementURL)
        XCTAssertEqual(settings.connection?.url, url)
        XCTAssertEqual(settings.apiKey, "")
        XCTAssertFalse(settings.isBusy)
        XCTAssertTrue(settings.canSave)
        let error = try XCTUnwrap(settings.errorMessage)
        XCTAssertTrue(error.contains("invalid_argument"))
        XCTAssertTrue(error.contains("[非表示]"))
        XCTAssertFalse(error.contains("example-secret-key"))
        XCTAssertTrue(error.contains("再入力"))
    }

    func testFailedKeyClearRetainsNonSensitiveChoice() {
        var settings = loaded()
        settings.setClearAPIKey(true)
        _ = settings.beginSave()
        settings.failed(code: "internal", message: "Unable to save")
        XCTAssertTrue(settings.clearAPIKey)
        XCTAssertEqual(settings.url, url)
        XCTAssertTrue(settings.canSave)
    }

    func testFailedDisableRetainsNonSensitiveEdits() {
        var settings = loaded()
        settings.url = replacementURL
        settings.setClearAPIKey(true)
        _ = settings.beginDisable()
        settings.failed(code: "internal", message: "Unable to save")
        XCTAssertEqual(settings.connection?.url, url)
        XCTAssertEqual(settings.url, replacementURL)
        XCTAssertTrue(settings.clearAPIKey)
        XCTAssertTrue(settings.canDisable)
    }

    func testFailedLoadAllowsRetryWithoutEnablingWrites() {
        var settings = GaiaConnectionSettings()
        settings.beginLoad()
        settings.failed(code: "transport", message: "Connection refused")
        XCTAssertFalse(settings.isLoaded)
        XCTAssertFalse(settings.canSave)
        XCTAssertFalse(settings.canTest)
        XCTAssertNotNil(settings.errorMessage)
        XCTAssertTrue(settings.beginLoad())
        settings.loaded(GaiaConnection(url: url, hasAPIKey: true, source: .saved))
        XCTAssertTrue(settings.canTest)
        XCTAssertNil(settings.errorMessage)
    }

    func testAllControlsAndDuplicateActionsAreDisabledDuringAnOperation() throws {
        var settings = loaded()
        let request = try XCTUnwrap(settings.beginTest())
        XCTAssertEqual(request.timeoutSeconds, 5)
        XCTAssertEqual(settings.operation, .testing)
        XCTAssertFalse(settings.canSave)
        XCTAssertFalse(settings.canDisable)
        XCTAssertFalse(settings.canTest)
        XCTAssertNil(settings.beginTest())
        XCTAssertNil(settings.beginSave())
        XCTAssertNil(settings.beginDisable())
        XCTAssertFalse(settings.beginLoad())
        settings.tested(try testResult())
        XCTAssertFalse(settings.isBusy)
        XCTAssertEqual(settings.currentTestResult?.client.defaultScope, "cloudnative")
    }

    func testUnsavedInputsCannotTestOrShowAnOldSuccess() throws {
        var settings = loaded()
        _ = settings.beginTest()
        settings.tested(try testResult())
        XCTAssertNotNil(settings.currentTestResult)
        settings.apiKey = "example-new-key"
        XCTAssertFalse(settings.canTest)
        XCTAssertNil(settings.beginTest())
        XCTAssertNil(settings.currentTestResult)
    }

    func testTestFailureKeepsConfigAndShowsUsefulTroubleshootingHint() {
        var settings = loaded()
        _ = settings.beginTest()
        settings.failed(code: "engine_unavailable", message: "Authentication failed")
        XCTAssertEqual(settings.connection?.url, url)
        XCTAssertTrue(settings.canTest)
        XCTAssertTrue(settings.errorMessage?.contains("API キー") ?? false)
        XCTAssertTrue(settings.errorMessage?.contains("Authentication failed") ?? false)
        XCTAssertNil(settings.currentTestResult)
    }

    func testDismissClearsInputAndIgnoresInFlightSaveCompletion() {
        var settings = loaded()
        settings.url = replacementURL
        settings.apiKey = "example-unsaved-key"
        _ = settings.beginSave()
        settings.dismiss()
        XCTAssertEqual(settings.apiKey, "")
        XCTAssertFalse(settings.isBusy)
        XCTAssertFalse(settings.hasUnsavedChanges)
        settings.saved(GaiaConnection(url: replacementURL, hasAPIKey: true, source: .saved))
        settings.failed(code: "transport", message: "Late failure")
        XCTAssertEqual(settings.connection?.url, url)
        XCTAssertNil(settings.errorMessage)
        XCTAssertNil(settings.notice)
    }
}
