import Foundation
import XCTest
@testable import NarumiMenuBarCore

final class ProviderSettingsStateTests: XCTestCase {
    func testSavedCredentialNeverPopulatesInputAndBlankUpdateRetainsIt() throws {
        var editor = ProviderConnectionSettings(connection: ProviderSettingsFixtures.connection())
        XCTAssertEqual(editor.apiKey, "")
        XCTAssertFalse(editor.canSave)
        editor.displayName = "New display name"
        let request = try XCTUnwrap(editor.takeSaveRequest())
        XCTAssertEqual(request.apiKey, .unchanged)
        XCTAssertEqual(request.expectedRevision, 1)
        XCTAssertNil(request.providerID)
        XCTAssertEqual(request.displayName, "New display name")
    }

    func testSecretIsClearedWhenRequestLeavesEditorAndExplicitClearIsDistinct() throws {
        var editor = ProviderConnectionSettings(connection: ProviderSettingsFixtures.connection())
        editor.apiKey = "not-a-real-key"
        XCTAssertEqual(try XCTUnwrap(editor.takeSaveRequest()).apiKey, .replace("not-a-real-key"))
        XCTAssertEqual(editor.apiKey, "")
        editor.apiKey = "another-fake-key"
        editor.setClearAPIKey(true)
        XCTAssertEqual(editor.apiKey, "")
        XCTAssertEqual(try XCTUnwrap(editor.takeSaveRequest()).apiKey, .clear)
    }

    func testProviderSwitchAndDismissClearSecretWithoutCopyingAmbientSettings() {
        var editor = ProviderConnectionSettings()
        editor.apiKey = "not-a-real-key"
        editor.selectProvider(.ollama)
        XCTAssertEqual(editor.apiKey, "")
        XCTAssertEqual(editor.endpoint, "http://127.0.0.1:11434")
        XCTAssertFalse(editor.usesAPIKey)
        editor.selectProvider(.claudeAgentSDK)
        editor.apiKey = "another-fake-key"
        editor.dismiss()
        XCTAssertEqual(editor.apiKey, "")
        XCTAssertEqual(editor.displayName, "")
    }

    func testDisableRetainsKeyAndStillSavesAnExplicitEnabledFalse() throws {
        var editor = ProviderConnectionSettings(connection: ProviderSettingsFixtures.connection())
        editor.enabled = false
        let request = try XCTUnwrap(editor.takeSaveRequest())
        XCTAssertEqual(request.enabled, false)
        XCTAssertEqual(request.apiKey, .unchanged)
    }

    func testOllamaRejectsRemoteAndCredentialBearingEndpoints() {
        var editor = ProviderConnectionSettings(providerID: .ollama)
        editor.displayName = "Local"
        for url in ["http://127.0.0.1:11434/", "http://[::1]:11434", "https://127.1.2.3:443", "https://[::1]:11434"] {
            editor.endpoint = url
            XCTAssertTrue(editor.canSave, url)
        }
        for url in ["http://localhost", "https://localhost", "http://example.com", "http://127.0.0.1@evil.example", "http://a:b@127.0.0.1", "http://127.0.0.1?key=x", "http://127.0.0.1/#x", "http://127.0.0.1:0", "http://127.0.0.1/api", "http://127.999.0.1", "http://128.0.0.1"] {
            editor.endpoint = url
            XCTAssertFalse(editor.canSave, url)
        }
    }

    func testUnknownErrorAndPriceNeverEchoRawTextOrAssumeZero() {
        let failure = ProviderSettingsFailure(code: "raw-secret-from-server")
        XCTAssertEqual(failure.code, .internalError)
        XCTAssertFalse(failure.message.contains("raw-secret"))
        XCTAssertFalse(ProviderDisplay.reason("raw-secret-from-server")?.contains("raw-secret") ?? true)
        XCTAssertEqual(ProviderDisplay.price(nil, unit: "token"), "不明（無料とは扱いません）")
    }

    func testMissingActiveAuthNeverClearsLostStartReceipt() throws {
        var recovery = ProviderSettingsRecovery()
        XCTAssertTrue(recovery.beginAuthentication(connectionID: ProviderSettingsFixtures.connectionID, requestID: "lost-start"))
        recovery.authenticationUnconfirmed(connectionID: ProviderSettingsFixtures.connectionID)
        recovery.observe(connections: [ProviderSettingsFixtures.connection()])
        let pending = try XCTUnwrap(recovery.authentications[ProviderSettingsFixtures.connectionID])
        XCTAssertEqual(pending.startRequestID, "lost-start")
        XCTAssertEqual(pending.state, .unknown)
        XCTAssertFalse(recovery.beginAuthentication(connectionID: ProviderSettingsFixtures.connectionID, requestID: "new-start"))
    }

    func testAuthenticationFromDifferentInstanceCannotResolveOriginalOperation() {
        var recovery = ProviderSettingsRecovery()
        recovery.observe(connections: [ProviderSettingsFixtures.connection(activeAuth: ProviderSettingsFixtures.activeAuth())])
        let result = ProviderSettingsFixtures.auth(
            state: .succeeded, instanceID: "00000000-0000-4000-8000-000000000002")
        XCTAssertFalse(recovery.receive(result))
        XCTAssertEqual(recovery.authentications[ProviderSettingsFixtures.connectionID]?.state, .unknown)
    }

    func testAuthenticationForAnotherRequestCannotOverwritePendingOriginal() {
        var recovery = ProviderSettingsRecovery()
        _ = recovery.beginAuthentication(connectionID: ProviderSettingsFixtures.connectionID, requestID: "original-start")
        XCTAssertFalse(recovery.receive(ProviderSettingsFixtures.auth(requestID: "other-start", state: .succeeded)))
        XCTAssertEqual(recovery.authentications[ProviderSettingsFixtures.connectionID]?.startRequestID, "original-start")
        XCTAssertEqual(recovery.authentications[ProviderSettingsFixtures.connectionID]?.state, .pending)
    }

    func testSetupRecoveryUsesMatchingLastReceiptRatherThanAnUnrelatedIdleSnapshot() throws {
        var recovery = ProviderSettingsRecovery()
        _ = recovery.beginSetup(providerID: .anthropicAPI, resourceID: "fixture-runtime", requestID: "lost-setup")
        recovery.setupUnconfirmed(providerID: .anthropicAPI)
        recovery.observe(providers: [ProviderSettingsFixtures.provider(lastSetup: ProviderSettingsFixtures.setup(requestID: "other"))])
        XCTAssertEqual(recovery.setups[.anthropicAPI]?.state, .unknown)
        XCTAssertNil(recovery.setups[.anthropicAPI]?.jobID)
        recovery.observe(providers: [ProviderSettingsFixtures.provider(lastSetup: ProviderSettingsFixtures.setup(requestID: "lost-setup", state: .succeeded))])
        let setup = try XCTUnwrap(recovery.setups[.anthropicAPI])
        XCTAssertEqual(setup.jobID, ProviderSettingsFixtures.jobID)
        XCTAssertEqual(setup.state, .succeeded)
        XCTAssertFalse(setup.unresolved)
    }

    func testWrongJobKindCannotProveRuntimePreparationSucceeded() {
        var recovery = ProviderSettingsRecovery()
        recovery.observe(providers: [ProviderSettingsFixtures.provider(activeSetup: ProviderSettingsFixtures.setup())])
        XCTAssertFalse(recovery.receive(ProviderSettingsFixtures.job(state: "succeeded", kind: "regenerate"), providerID: .anthropicAPI))
        XCTAssertEqual(recovery.setups[.anthropicAPI]?.state, .running)
    }

    func testNewActiveOperationsAreRecoveredAfterPreviousOperationFinished() {
        var recovery = ProviderSettingsRecovery()
        recovery.observe(providers: [ProviderSettingsFixtures.provider(lastSetup: ProviderSettingsFixtures.setup(state: .succeeded))])
        recovery.observe(providers: [ProviderSettingsFixtures.provider(activeSetup: ProviderSettingsFixtures.setup(requestID: "new-setup"))])
        XCTAssertEqual(recovery.setups[.anthropicAPI]?.startRequestID, "new-setup")
        _ = recovery.beginAuthentication(connectionID: ProviderSettingsFixtures.connectionID, requestID: "original-start")
        XCTAssertTrue(recovery.receive(ProviderSettingsFixtures.auth(state: .succeeded)))
        recovery.observe(connections: [ProviderSettingsFixtures.connection(activeAuth: ProviderSettingsFixtures.activeAuth(requestID: "new-start"))])
        XCTAssertEqual(recovery.authentications[ProviderSettingsFixtures.connectionID]?.startRequestID, "new-start")
    }
}
