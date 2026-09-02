import Foundation
import XCTest
@testable import NarumiMenuBarCore

final class ProviderSettingsStateTests: XCTestCase {
    func testSixProvidersUseCanonicalPickerOrderAndNames() {
        XCTAssertEqual(ProviderID.connectionPickerOrder, [
            .codexAppServer, .claudeAgentSDK, .openaiAPI, .openAICompatibleAPI, .anthropicAPI, .ollama,
        ])
        XCTAssertEqual(ProviderID.connectionPickerOrder.map(ProviderDisplay.name), [
            "Codex App Server", "Claude Agent SDK", "OpenAI API", "OpenAI互換API", "Anthropic API", "Ollama",
        ])
        XCTAssertEqual(ProviderID.openAICompatibleAPI.rawValue, "openai-compatible-api")
        XCTAssertEqual(ProviderID.openAICompatibleAPI.supportedAuthMethods, [.apiKey, .none])
    }

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

    func testCodexEditorSavesWithoutAPIKeyAndCannotOverrideItsDestination() throws {
        var editor = ProviderConnectionSettings()
        editor.apiKey = "fixture-anthropic-key"
        editor.selectProvider(.codexAppServer)
        XCTAssertEqual(editor.apiKey, "")
        XCTAssertFalse(editor.usesAPIKey)
        XCTAssertEqual(editor.endpoint, "https://chatgpt.com")
        editor.displayName = "Meeting Codex"
        editor.endpoint = "https://example.invalid"
        XCTAssertTrue(editor.canSave)
        let request = try XCTUnwrap(editor.takeSaveRequest())
        XCTAssertEqual(request.providerID, .codexAppServer)
        XCTAssertEqual(request.authMethod, .chatgpt)
        XCTAssertEqual(request.endpoint, "https://chatgpt.com")
        XCTAssertEqual(request.apiKey, .unchanged)
        let object = try XCTUnwrap(JSONSerialization.jsonObject(with: JSONEncoder().encode(request)) as? [String: Any])
        XCTAssertNil(object["api_key"])
        editor.adopt(ProviderSettingsFixtures.connection(providerID: .codexAppServer, credential: false))
        editor.setClearAPIKey(true)
        XCTAssertFalse(editor.clearAPIKey)
        XCTAssertFalse(editor.canSave)
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

    func testOpenAIEditorClearsOtherProviderSecretAndPinsDestination() throws {
        var editor = ProviderConnectionSettings()
        editor.apiKey = "fixture-other-key"
        editor.selectProvider(.openaiAPI)
        XCTAssertEqual(editor.apiKey, "")
        XCTAssertTrue(editor.usesAPIKey)
        XCTAssertEqual(editor.endpoint, "https://api.openai.com")
        editor.displayName = "Meeting API"
        editor.endpoint = "https://example.invalid"
        editor.apiKey = "fixture-openai-key"
        let request = try XCTUnwrap(editor.takeSaveRequest())
        XCTAssertEqual(request.providerID, .openaiAPI)
        XCTAssertEqual(request.endpoint, "https://api.openai.com")
        XCTAssertEqual(request.authMethod, .apiKey)
        XCTAssertNil(request.apiSurface, "official OpenAI must stay encodable for contract 4/5 servers")
        XCTAssertEqual(request.apiKey, .replace("fixture-openai-key"))
        let encoded = try XCTUnwrap(JSONSerialization.jsonObject(
            with: JSONEncoder().encode(request)) as? [String: Any])
        XCTAssertNil(encoded["api_surface"])
        XCTAssertNil(encoded["chat_max_tokens_field"])
        XCTAssertEqual(editor.apiKey, "")
        editor.adopt(ProviderSettingsFixtures.connection(providerID: .openaiAPI))
        XCTAssertEqual(editor.apiKey, "")
        editor.displayName = "Updated API connection"
        XCTAssertEqual(try XCTUnwrap(editor.takeSaveRequest()).apiKey, .unchanged)
        editor.setClearAPIKey(true)
        XCTAssertEqual(try XCTUnwrap(editor.takeSaveRequest()).apiKey, .clear)
    }

    func testOpenAICompatibleEditorValidatesDestinationAuthAndSurface() throws {
        var editor = ProviderConnectionSettings(providerID: .openAICompatibleAPI)
        editor.displayName = "Compatible API"
        editor.endpoint = "https://compatible.example.invalid/v1"
        XCTAssertTrue(editor.canSave)
        editor.selectAuthMethod(.none)
        XCTAssertFalse(editor.canSave, "remote no-auth must be rejected")
        editor.endpoint = "http://127.0.0.1:8080/v1"
        XCTAssertTrue(editor.canSave, "numeric loopback may use HTTP without auth")
        editor.apiSurface = .chatCompletions
        editor.chatMaxTokensField = .maxCompletionTokens
        let request = try XCTUnwrap(editor.takeSaveRequest(requestID: "compatible-create"))
        XCTAssertEqual(request.providerID, .openAICompatibleAPI)
        XCTAssertEqual(request.authMethod, ProviderAuthMethod.none)
        XCTAssertEqual(request.apiSurface, .chatCompletions)
        XCTAssertEqual(request.chatMaxTokensField, .maxCompletionTokens)
        let object = try XCTUnwrap(JSONSerialization.jsonObject(
            with: JSONEncoder().encode(request)) as? [String: Any])
        XCTAssertEqual(object["endpoint"] as? String, "http://127.0.0.1:8080/v1")
        XCTAssertEqual(object["api_surface"] as? String, "chat_completions")
        XCTAssertEqual(object["chat_max_tokens_field"] as? String, "max_completion_tokens")
        XCTAssertNil(object["api_key"])
    }

    func testOpenAICompatibleEndpointRejectsObviousUnsafeShapes() {
        let valid = [
            "https://api.example.com/v1", "https://LLM.Example.COM:443/api-v1/models_v2~beta",
            "http://127.0.0.1:8080/v1", "https://[::1]:8443/api/v1",
        ]
        for endpoint in valid {
            XCTAssertTrue(ProviderConnectionSettings.isCompatibleEndpointValid(endpoint, authMethod: .apiKey), endpoint)
        }
        let invalid = [
            "http://api.example.com/v1", "https://api.example.com/v1/", "https://user@api.example.com/v1",
            "https://api.example.com/v1?key=x", "https://api.example.com/v1#x", "https://api.example.com/v1//chat",
            "https://api.example.com/v1/../chat", "https://api.example.com/%76%31", "https://localhost/v1",
            "https://127.0.0.1:080/v1", "https://127.00.0.1/v1", "https://api.example.com/v1/@chat",
            "https://api.example.com/-v1", "https://api..example.com/v1", "https://-api.example.com/v1",
            "https://api.example.com:/v1", "https://api.example.com:65536/v1",
            "HTTPS://api.example.com/v1",
            "https://api.example.com/" + String(repeating: "a", count: 129),
        ]
        for endpoint in invalid {
            XCTAssertFalse(ProviderConnectionSettings.isCompatibleEndpointValid(endpoint, authMethod: .apiKey), endpoint)
        }
        XCTAssertFalse(ProviderConnectionSettings.isCompatibleEndpointValid(
            "https://api.example.com/v1", authMethod: .none))
    }

    func testCompatibleSwitchToNoAuthExplicitlyClearsSavedCredential() throws {
        var editor = ProviderConnectionSettings(connection: ProviderSettingsFixtures.connection(
            providerID: .openAICompatibleAPI, credential: true))
        editor.endpoint = "http://127.0.0.1:8080/v1"
        editor.selectAuthMethod(.none)
        let request = try XCTUnwrap(editor.takeSaveRequest(requestID: "compatible-no-auth"))
        XCTAssertEqual(request.apiKey, .clear)
        let object = try XCTUnwrap(JSONSerialization.jsonObject(
            with: JSONEncoder().encode(request)) as? [String: Any])
        XCTAssertTrue(object["api_key"] is NSNull)
    }

    func testCompatibleEndpointChangeNeverReusesThePreviousAPIKey() throws {
        let connection = ProviderSettingsFixtures.connection(
            providerID: .openAICompatibleAPI, credential: true)
        var editor = ProviderConnectionSettings(connection: connection)
        editor.endpoint = "https://other.example.invalid/v1"
        XCTAssertTrue(editor.requiresAPIKeyReentryForEndpointChange)
        XCTAssertFalse(editor.canSave)
        editor.apiKey = "fixture-key-for-new-endpoint"
        XCTAssertTrue(editor.canSave)
        XCTAssertEqual(
            try XCTUnwrap(editor.takeSaveRequest()).apiKey,
            .replace("fixture-key-for-new-endpoint"))

        var clearing = ProviderConnectionSettings(connection: connection)
        clearing.endpoint = "https://other.example.invalid/v1"
        clearing.setClearAPIKey(true)
        XCTAssertFalse(clearing.requiresAPIKeyReentryForEndpointChange)
        XCTAssertTrue(clearing.canSave)
        XCTAssertEqual(try XCTUnwrap(clearing.takeSaveRequest()).apiKey, .clear)
    }

    func testCompatibleEndpointEditClearsAKeyTypedForThePreviousDestination() throws {
        var editor = ProviderConnectionSettings(providerID: .openAICompatibleAPI)
        editor.displayName = "Compatible API"
        editor.endpoint = "https://first.example.invalid/v1"
        editor.apiKey = "fixture-key-for-first-endpoint"

        editor.endpoint = "https://second.example.invalid/v1"

        XCTAssertTrue(editor.apiKey.isEmpty)
        XCTAssertFalse(editor.canSave, "the key must be entered again for the final endpoint")
        editor.apiKey = "fixture-key-for-second-endpoint"
        let request = try XCTUnwrap(editor.takeSaveRequest(requestID: "compatible-endpoint-change"))
        XCTAssertEqual(request.endpoint, "https://second.example.invalid/v1")
        XCTAssertEqual(request.apiKey, .replace("fixture-key-for-second-endpoint"))
    }

    func testCompatibleUpdateExplicitlyClearsChatTokenFieldForResponses() throws {
        let connection = ProviderSettingsFixtures.connection(
            providerID: .openAICompatibleAPI, apiSurface: .chatCompletions,
            chatMaxTokensField: .maxCompletionTokens)
        var editor = ProviderConnectionSettings(connection: connection)
        editor.apiSurface = .responses
        let request = try XCTUnwrap(editor.takeSaveRequest(requestID: "compatible-update"))
        XCTAssertTrue(request.clearsChatMaxTokensField)
        let object = try XCTUnwrap(JSONSerialization.jsonObject(
            with: JSONEncoder().encode(request)) as? [String: Any])
        XCTAssertEqual(object["api_surface"] as? String, "responses")
        XCTAssertTrue(object["chat_max_tokens_field"] is NSNull)
    }

    func testOpenAIMetadataMessagesDistinguishGenerationAndBalanceChecks() throws {
        let scope = ProviderDisplay.connectionVerificationScope(.openaiAPI)
        XCTAssertTrue(scope.contains("https://api.openai.com/v1/models"))
        XCTAssertTrue(scope.contains("会議データの送信・議事録生成は行いません"))
        let result = ProviderConnectionTestResult(
            connection: ProviderSettingsFixtures.connection(providerID: .openaiAPI), connected: true,
            reason: "model_list_verified_generation_unchecked")
        XCTAssertEqual(
            ProviderDisplay.connectionTestResult(result),
            "モデル一覧を取得できました。残高・生成権限・議事録生成の成功は未確認です。")
        XCTAssertEqual(
            ProviderDisplay.reason(result.reason),
            "モデル一覧の取得を確認しました。残高・生成権限・実際の議事録生成は未確認です。")
    }

    func testUnknownErrorAndPriceNeverEchoRawTextOrAssumeZero() {
        let failure = ProviderSettingsFailure(code: "raw-secret-from-server")
        XCTAssertEqual(failure.code, .internalError)
        XCTAssertFalse(failure.message.contains("raw-secret"))
        XCTAssertFalse(ProviderDisplay.reason("raw-secret-from-server")?.contains("raw-secret") ?? true)
        XCTAssertEqual(ProviderDisplay.price(nil, unit: "token"), "不明（無料とは扱いません）")
    }

    func testBundledRuntimeFailureExplainsRecoveryAndNoFallback() {
        let message = ProviderDisplay.reason("bundled_runtime_unavailable")
        XCTAssertTrue(message?.contains("更新または再インストール") == true)
        XCTAssertTrue(message?.contains("外部の実行環境へは切り替えません") == true)
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

    func testLoginURLSurvivesMatchingSnapshotButIsClearedWhenUnconfirmedOrFinished() throws {
        let url = try XCTUnwrap(ProviderAuthorizationURL(ProviderSettingsFixtures.authorizationURL()))
        var recovery = ProviderSettingsRecovery()
        _ = recovery.beginAuthentication(connectionID: ProviderSettingsFixtures.connectionID, requestID: "original-start")
        XCTAssertTrue(recovery.receive(ProviderSettingsFixtures.auth(authorizationURL: url)))
        recovery.observe(connections: [ProviderSettingsFixtures.connection(
            providerID: .codexAppServer, activeAuth: ProviderSettingsFixtures.activeAuth())])
        XCTAssertEqual(recovery.authentications[ProviderSettingsFixtures.connectionID]?.authorizationURL, url)
        XCTAssertEqual(recovery.authentications[ProviderSettingsFixtures.connectionID]?.userCode?.displayValue, ProviderSettingsFixtures.userCode)
        recovery.authenticationUnconfirmed(connectionID: ProviderSettingsFixtures.connectionID)
        XCTAssertNil(recovery.authentications[ProviderSettingsFixtures.connectionID]?.authorizationURL)
        XCTAssertNil(recovery.authentications[ProviderSettingsFixtures.connectionID]?.userCode)
        XCTAssertTrue(recovery.receive(ProviderSettingsFixtures.auth(authorizationURL: url)))
        XCTAssertEqual(recovery.authentications[ProviderSettingsFixtures.connectionID]?.authorizationURL, url)
        XCTAssertTrue(recovery.receive(ProviderSettingsFixtures.auth(state: .succeeded)))
        XCTAssertNil(recovery.authentications[ProviderSettingsFixtures.connectionID]?.authorizationURL)
        XCTAssertNil(recovery.authentications[ProviderSettingsFixtures.connectionID]?.userCode)
    }

    func testLoginURLIsNotRetainedAcrossRevisionOrServerIdentityMismatch() throws {
        let url = try XCTUnwrap(ProviderAuthorizationURL(ProviderSettingsFixtures.authorizationURL()))
        for operation in [
            ProviderSettingsFixtures.auth(instanceID: "00000000-0000-4000-8000-000000000002", authorizationURL: url),
            ProviderSettingsFixtures.auth(connectionRevision: 2, authorizationURL: url),
        ] {
            var recovery = ProviderSettingsRecovery()
            _ = recovery.beginAuthentication(connectionID: ProviderSettingsFixtures.connectionID, requestID: "original-start")
            XCTAssertTrue(recovery.receive(ProviderSettingsFixtures.auth(authorizationURL: url)))
            XCTAssertFalse(recovery.receive(operation))
            XCTAssertEqual(recovery.authentications[ProviderSettingsFixtures.connectionID]?.state, .unknown)
            XCTAssertNil(recovery.authentications[ProviderSettingsFixtures.connectionID]?.authorizationURL)
            XCTAssertNil(recovery.authentications[ProviderSettingsFixtures.connectionID]?.userCode)
        }
    }

    func testDeviceChallengeClearsForEveryCompletionStateAndRejectsAnIncompletePair() throws {
        let url = try XCTUnwrap(ProviderAuthorizationURL(ProviderSettingsFixtures.authorizationURL()))
        for state in [ProviderAuthOperationState.succeeded, .failed, .cancelled, .unknown] {
            var recovery = ProviderSettingsRecovery()
            _ = recovery.beginAuthentication(connectionID: ProviderSettingsFixtures.connectionID, requestID: "original-start")
            XCTAssertTrue(recovery.receive(ProviderSettingsFixtures.auth(authorizationURL: url)))
            XCTAssertNotNil(recovery.authentications[ProviderSettingsFixtures.connectionID]?.userCode)
            XCTAssertTrue(recovery.receive(ProviderSettingsFixtures.auth(state: state)))
            XCTAssertNil(recovery.authentications[ProviderSettingsFixtures.connectionID]?.authorizationURL)
            XCTAssertNil(recovery.authentications[ProviderSettingsFixtures.connectionID]?.userCode)
        }
        var recovery = ProviderSettingsRecovery()
        _ = recovery.beginAuthentication(connectionID: ProviderSettingsFixtures.connectionID, requestID: "original-start")
        let code = try XCTUnwrap(ProviderUserCode(ProviderSettingsFixtures.userCode))
        XCTAssertFalse(recovery.receive(ProviderSettingsFixtures.auth(userCode: code)))
        XCTAssertEqual(recovery.authentications[ProviderSettingsFixtures.connectionID]?.state, .unknown)
        XCTAssertNil(recovery.authentications[ProviderSettingsFixtures.connectionID]?.userCode)
    }

    func testInactiveOrReplacedLoginSnapshotDiscardsChallengeWithoutResolvingOriginalReceipt() throws {
        let url = try XCTUnwrap(ProviderAuthorizationURL(ProviderSettingsFixtures.authorizationURL()))
        let snapshots: [[ProviderConnection]] = [
            [ProviderSettingsFixtures.connection(providerID: .codexAppServer)],
            [ProviderSettingsFixtures.connection(
                providerID: .codexAppServer, activeAuth: ProviderSettingsFixtures.activeAuth(requestID: "different-start"))],
            [],
        ]
        for connections in snapshots {
            var recovery = ProviderSettingsRecovery()
            _ = recovery.beginAuthentication(connectionID: ProviderSettingsFixtures.connectionID, requestID: "original-start")
            XCTAssertTrue(recovery.receive(ProviderSettingsFixtures.auth(authorizationURL: url)))
            recovery.observe(connections: connections)
            let original = try XCTUnwrap(recovery.authentications[ProviderSettingsFixtures.connectionID])
            XCTAssertEqual(original.startRequestID, "original-start")
            XCTAssertEqual(original.state, .pending)
            XCTAssertNil(original.authorizationURL)
            XCTAssertNil(original.userCode)
        }
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
