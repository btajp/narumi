import XCTest
@testable import NarumiMenuBarCore

final class MinutesModelFormTests: XCTestCase {
    func testMeetingSaveReloadPreservesPinnedSelectionAndLegacyStages() throws {
        let expected = MinutesModelFixtures.config(cacheEpoch: 3)
        let form = MeetingConfigurationForm(detail: try MinutesModelFixtures.detail(config: expected))
        let update = try form.processing.makeUpdate()
        let saved = try JSONEncoder().encode(update)
        let reloaded = try JSONDecoder().decode(MeetingConfig.self, from: saved)
        XCTAssertEqual(reloaded, expected)
        XCTAssertEqual(reloaded.llmProvider, "none")
        XCTAssertEqual(reloaded.transcriptionEngine, "auto")
        XCTAssertEqual(reloaded.diarizationEngine, "none")
        XCTAssertEqual(form.processing.minutesModel.reasoningEffort, "high")
    }

    func testProfileAndNewMeetingShareTheExactSelection() throws {
        let config = MinutesModelFixtures.config()
        let profile = Profile(
            name: "meeting-profile", config: config, scope: "work", engagement: "Fixture",
            exportDestinations: ["markdown"], isDefault: true)
        let form = ProfileConfigurationForm(profile: profile)
        let reloaded = try JSONDecoder().decode(MeetingConfig.self, from: JSONEncoder().encode(form.processing.makeUpdate()))
        let meeting = MeetingConfigurationForm(detail: try MinutesModelFixtures.detail(config: reloaded))
        XCTAssertEqual(meeting.processing.minutesModel.selection, config.minutesModel)
        XCTAssertEqual(form.processing.minutesModel, meeting.processing.minutesModel)
        XCTAssertEqual(form.scope, "work")
        XCTAssertEqual(form.exportDestinations, ["markdown"])
        XCTAssertTrue(form.makeDefault)
        XCTAssertFalse(form.isNew)
    }

    func testReturningToLegacyWritesExplicitNullAndDoesNotChangeLLMProvider() throws {
        var form = ProcessingConfigurationForm(config: MinutesModelFixtures.config())
        form.minutesModel.mode = .legacy
        let data = try JSONEncoder().encode(form.makeUpdate())
        let object = try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
        XCTAssertTrue(object["minutes_model"] is NSNull)
        XCTAssertEqual(object["llm_provider"] as? String, "none")
        XCTAssertNil(try JSONDecoder().decode(MeetingConfig.self, from: data).minutesModel)
    }

    func testOldConfigsKeepLegacyBehaviorAndUnspecifiedEngineFields() throws {
        let config = try JSONDecoder().decode(MeetingConfig.self, from: Data(#"{"llm_provider":"ollama"}"#.utf8))
        let form = ProcessingConfigurationForm(config: config)
        XCTAssertEqual(form.minutesModel.mode, .legacy)
        XCTAssertNil(form.minutesModel.selection)
        let data = try JSONEncoder().encode(form.makeUpdate())
        let object = try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
        XCTAssertNil(object["transcription_engine"])
        XCTAssertNil(object["external_send_policy"])
        XCTAssertTrue(object["minutes_model"] is NSNull)
    }

    func testLegacySaveOmitsNewFieldForContractTwoButCannotSendACodexSelection() throws {
        let form = ProcessingConfigurationForm(config: MeetingConfig(llmProvider: "none"))
        let data = try JSONEncoder().encode(form.makeUpdate(supportsMinutesModel: false))
        let object = try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
        XCTAssertNil(object["minutes_model"])
        XCTAssertEqual(object["llm_provider"] as? String, "none")
        let codex = ProcessingConfigurationForm(config: MinutesModelFixtures.config())
        XCTAssertThrowsError(try codex.makeUpdate(supportsMinutesModel: false))
    }

    func testDraftNeedsExplicitConnectionAndModelBeforeEncoding() throws {
        var form = ProcessingConfigurationForm()
        form.minutesModel.mode = .selected
        XCTAssertThrowsError(try form.makeUpdate())
        form.minutesModel.selectConnection(MinutesModelFixtures.connection())
        XCTAssertThrowsError(try form.makeUpdate())
        form.minutesModel.selectModel(MinutesModelFixtures.model())
        XCTAssertNotNil(try form.makeUpdate().config.minutesModel)
    }

    func testSelectionDoesNotRaisePolicyAutomatically() {
        var form = ProcessingConfigurationForm()
        form.minutesModel.mode = .selected
        form.minutesModel.selectConnection(MinutesModelFixtures.connection())
        form.minutesModel.selectModel(MinutesModelFixtures.model())
        XCTAssertEqual(form.externalSendPolicy, "")
        XCTAssertEqual(form.effectiveExternalSendPolicy, "local_only")
        XCTAssertNotNil(validate(form))
        form.externalSendPolicy = "subscription_ok"
        XCTAssertNil(validate(form))
        form.externalSendPolicy = "api_ok"
        XCTAssertNil(validate(form))
    }

    func testBlankPolicyRetainsSavedPolicyInsteadOfBroadeningIt() {
        var form = ProcessingConfigurationForm(config: MinutesModelFixtures.config(policy: "local_only"))
        form.externalSendPolicy = ""
        XCTAssertEqual(form.effectiveExternalSendPolicy, "local_only")
        XCTAssertNotNil(validate(form))
        var allowed = ProcessingConfigurationForm(config: MinutesModelFixtures.config())
        allowed.externalSendPolicy = ""
        XCTAssertEqual(allowed.effectiveExternalSendPolicy, "subscription_ok")
        XCTAssertNil(validate(allowed))
    }

    func testConnectionRevisionChangeRequiresExplicitReselection() {
        var form = MinutesModelForm(selection: MinutesModelFixtures.selection())
        let newConnection = MinutesModelFixtures.connection(revision: 2)
        XCTAssertNotNil(form.validationMessage(
            connections: [newConnection], catalog: MinutesModelFixtures.catalog(revision: 2),
            externalSendPolicy: "subscription_ok"))
        XCTAssertEqual(form.connectionRevision, 1)
        form.selectConnection(newConnection)
        XCTAssertEqual(form.connectionRevision, 2)
        XCTAssertTrue(form.modelID.isEmpty)
        XCTAssertTrue(form.reasoningEffort.isEmpty)
        form.selectModel(MinutesModelFixtures.model())
        XCTAssertNil(form.validationMessage(
            connections: [newConnection], catalog: MinutesModelFixtures.catalog(revision: 2),
            externalSendPolicy: "subscription_ok"))
    }

    func testDeletedDisabledUnauthenticatedAndPendingConnectionsCannotSave() {
        let form = MinutesModelForm(selection: MinutesModelFixtures.selection())
        for connections in [[], [MinutesModelFixtures.connection(enabled: false)],
                            [MinutesModelFixtures.connection(authState: .unconfigured)],
                            [ProviderSettingsFixtures.connection(
                                providerID: .codexAppServer, authState: .authenticated,
                                activeAuth: ProviderSettingsFixtures.activeAuth())]] {
            XCTAssertNotNil(form.validationMessage(
                connections: connections, catalog: MinutesModelFixtures.catalog(), externalSendPolicy: "subscription_ok"))
        }
    }

    func testUnknownUnavailableNonTextAndAPIModelsCannotSave() {
        let form = MinutesModelForm(selection: MinutesModelFixtures.selection())
        let models = [
            MinutesModelFixtures.model(id: "different-model"), MinutesModelFixtures.model(availability: .unverified),
            MinutesModelFixtures.model(availability: .retired), MinutesModelFixtures.model(inputs: [.image]),
            MinutesModelFixtures.model(outputs: [.audio]), MinutesModelFixtures.model(billing: .api),
        ]
        for model in models {
            XCTAssertNotNil(form.validationMessage(
                connections: [MinutesModelFixtures.connection()], catalog: MinutesModelFixtures.catalog(models: [model]),
                externalSendPolicy: "subscription_ok"))
        }
        for state in [ProviderCatalogState.unfetched, .stale, .failed, .authenticationRequired] {
            XCTAssertNotNil(form.validationMessage(
                connections: [MinutesModelFixtures.connection()], catalog: MinutesModelFixtures.catalog(state: state),
                externalSendPolicy: "subscription_ok"))
        }
    }

    func testReasoningEffortComesOnlyFromTheSelectedModelsCatalog() {
        var form = ProcessingConfigurationForm(config: MinutesModelFixtures.config())
        XCTAssertEqual(MinutesModelForm.reasoningOptions(MinutesModelFixtures.model()), ["low", "high"])
        XCTAssertNil(validate(form))
        form.minutesModel.reasoningEffort = "unsupported"
        XCTAssertNotNil(validate(form))
        form.minutesModel.reasoningEffort = ""
        XCTAssertNil(validate(form))
        form.minutesModel.selectModel(MinutesModelFixtures.model(id: "another-model"))
        XCTAssertTrue(form.minutesModel.reasoningEffort.isEmpty)
    }

    func testNewAttemptIsExplicitAndSurvivesSaveReload() throws {
        var form = ProcessingConfigurationForm(config: MinutesModelFixtures.config(cacheEpoch: 4))
        XCTAssertEqual(form.minutesModel.cacheEpoch, 4)
        _ = try form.makeUpdate()
        XCTAssertEqual(form.minutesModel.cacheEpoch, 4)
        form.minutesModel.prepareNewAttempt()
        XCTAssertEqual(form.minutesModel.cacheEpoch, 5)
        let config = try JSONDecoder().decode(MeetingConfig.self, from: JSONEncoder().encode(form.makeUpdate()))
        XCTAssertEqual(config.minutesModel?.cacheEpoch, 5)
        XCTAssertEqual(config.llmProvider, "none")
    }

    func testSelectionDecodesContractDefaultsButRejectsInvalidValues() throws {
        let minimal: [String: Any] = [
            "provider": "codex-app-server", "connection_id": MinutesModelFixtures.connectionID,
            "connection_revision": 1, "model_id": MinutesModelFixtures.modelID,
        ]
        let selection = try JSONDecoder().decode(
            MinutesModelSelection.self, from: JSONSerialization.data(withJSONObject: minimal))
        XCTAssertEqual(selection.cacheEpoch, 0)
        XCTAssertNil(selection.parameters.reasoningEffort)
        for (key, value) in [
            ("provider", "claude-agent-sdk" as Any), ("connection_revision", 0), ("cache_epoch", -1),
            ("model_id", ""), ("parameters", NSNull()),
            ("parameters", ["reasoning_effort": "wrong value"]),
            ("parameters", ["reasoning_effort": NSNull()]),
            ("parameters", ["command": "fixture"]), ("endpoint", "https://example.invalid"),
        ] {
            var invalid = minimal
            invalid[key] = value
            XCTAssertThrowsError(try JSONDecoder().decode(
                MinutesModelSelection.self, from: JSONSerialization.data(withJSONObject: invalid)))
        }
    }

    func testInvalidProgrammaticSelectionCannotBeEncoded() {
        var selection = MinutesModelFixtures.selection()
        selection.connectionRevision = 0
        XCTAssertThrowsError(try JSONEncoder().encode(selection))
    }

    func testSwitchingSavedModelsOrRevisionsChangesTheCachedReadIdentity() {
        let original = MinutesModelForm(selection: MinutesModelFixtures.selection())
        var anotherModel = original
        anotherModel.selectModel(MinutesModelFixtures.model(id: "next-page-model"))
        XCTAssertNotEqual(anotherModel.catalogReadIdentity, original.catalogReadIdentity)
        let anotherRevision = MinutesModelForm(selection: MinutesModelFixtures.selection(revision: 2))
        XCTAssertNotEqual(anotherRevision.catalogReadIdentity, original.catalogReadIdentity)
        var sameCatalog = original
        sameCatalog.reasoningEffort = "low"
        sameCatalog.prepareNewAttempt()
        XCTAssertEqual(sameCatalog.catalogReadIdentity, original.catalogReadIdentity)
    }

    func testEverySupportedProviderSavesAndReloadsExactMeetingAndProfileParameters() throws {
        for provider in MinutesModelSelection.providers {
            let selection = MinutesModelSelection(
                provider: provider, connectionID: MinutesModelFixtures.connectionID, connectionRevision: 7,
                modelID: "fixture-\(provider)", reasoningEffort: ["codex-app-server", "openai-api"].contains(provider) ? "high" : nil,
                maxTokens: provider == "codex-app-server" ? nil : 2048, cacheEpoch: 5)
            let policy = provider == "ollama" ? "local_only" : (provider == "codex-app-server" ? "subscription_ok" : "api_ok")
            let config = MeetingConfig(
                transcriptionEngine: "auto", diarizationEngine: "none", llmProvider: "none",
                externalSendPolicy: policy, vocabHints: [], minutesModel: selection)
            let form = ProcessingConfigurationForm(config: config)
            let reloaded = try JSONDecoder().decode(MeetingConfig.self, from: JSONEncoder().encode(form.makeUpdate()))
            XCTAssertEqual(reloaded, config, provider)
            let profile = Profile(
                name: "fixture-profile", config: config, scope: nil, engagement: nil,
                exportDestinations: [], isDefault: false)
            let profileForm = ProfileConfigurationForm(profile: profile)
            XCTAssertEqual(profileForm.processing.minutesModel.selection, selection, provider)
            var retry = form
            retry.minutesModel.prepareNewAttempt()
            XCTAssertEqual(retry.minutesModel.selection?.cacheEpoch, 6, provider)
            XCTAssertEqual(retry.minutesModel.selection?.parameters, selection.parameters, provider)
            XCTAssertEqual(retry.externalSendPolicy, policy, provider)
        }
    }

    func testProvidersNeverInheritUnsupportedParameters() throws {
        let base: [String: Any] = [
            "provider": "openai-api", "connection_id": MinutesModelFixtures.connectionID,
            "connection_revision": 1, "model_id": "fixture-text-model",
        ]
        for (provider, parameters) in [
            ("codex-app-server", ["max_tokens": 512] as [String: Any]),
            ("anthropic-api", ["reasoning_effort": "high"]),
            ("ollama", ["reasoning_effort": "high"]),
            ("openai-api", ["max_tokens": 0]),
            ("openai-api", ["max_tokens": 32769]),
            ("openai-api", ["max_tokens": 2.5]),
            ("openai-api", ["max_tokens": true]),
            ("openai-api", ["max_tokens": NSNull()]),
            ("openai-api", ["api_key": "fixture-never-a-real-key"]),
        ] {
            var object = base
            object["provider"] = provider
            object["parameters"] = parameters
            XCTAssertThrowsError(try JSONDecoder().decode(
                MinutesModelSelection.self, from: JSONSerialization.data(withJSONObject: object)), provider)
        }
        for value in [1, 32768] {
            var object = base
            object["parameters"] = ["max_tokens": value]
            let decoded = try JSONDecoder().decode(
                MinutesModelSelection.self, from: JSONSerialization.data(withJSONObject: object))
            XCTAssertEqual(decoded.parameters.maxTokens, value)
        }
    }

    func testAPIPolicyMustBeExplicitAndOllamaCanStayLocal() {
        for provider in ["openai-api", "anthropic-api", "ollama"] {
            let connection = MinutesModelFixtures.connection(providerID: ProviderID(rawValue: provider)!)
            let model = MinutesModelFixtures.model(provider: provider)
            var form = ProcessingConfigurationForm()
            form.minutesModel.mode = .selected
            form.minutesModel.selectConnection(connection)
            form.minutesModel.selectModel(model)
            XCTAssertEqual(form.effectiveExternalSendPolicy, "local_only")
            let check = { (policy: String) in form.minutesModel.validationMessage(
                connections: [connection], catalog: MinutesModelFixtures.catalog(models: [model]),
                externalSendPolicy: policy) }
            if provider == "ollama" {
                XCTAssertNil(check("local_only"))
            } else {
                XCTAssertNotNil(check("local_only"), provider)
                XCTAssertNotNil(check("subscription_ok"), provider)
            }
            XCTAssertNil(check("api_ok"), provider)
        }
    }

    func testOutputLimitValidatesIntegerRangeSchemaAndKnownCapability() {
        let provider = "openai-api"
        let connection = MinutesModelFixtures.connection(providerID: .openaiAPI)
        let model = MinutesModelFixtures.model(provider: provider, maxOutputTokens: 8192)
        var form = MinutesModelForm(selection: MinutesModelSelection(
            provider: provider, connectionID: connection.connectionID, connectionRevision: 1, modelID: model.modelID))
        func check(_ candidate: ProviderModelDescriptor) -> String? {
            form.validationMessage(
                connections: [connection], catalog: MinutesModelFixtures.catalog(models: [candidate]),
                externalSendPolicy: "api_ok")
        }
        XCTAssertEqual(form.effectiveOutputLimit(model), 4096)
        XCTAssertNil(form.selection?.parameters.maxTokens)
        XCTAssertNil(check(model))
        for invalid in ["0", "-1", "32769", "4.5", "four", " 4096", "4096 ", "99999999999999999999"] {
            form.maxTokensText = invalid
            XCTAssertNotNil(check(model), invalid)
            XCTAssertNil(form.selection, invalid)
        }
        form.maxTokensText = "8193"
        XCTAssertNotNil(check(model))
        form.maxTokensText = "8192"
        XCTAssertNil(check(model))
        let schema = ProviderParameterSchema(properties: [
            "max_tokens": ProviderModelParameter(type: .integer, enumValues: [.number(1024)], minimum: 512, maximum: 2048),
        ])
        let constrained = MinutesModelFixtures.model(provider: provider, parameterSchema: schema)
        XCTAssertNotNil(check(constrained))
        form.maxTokensText = "1024"
        XCTAssertNil(check(constrained))
        form.reasoningEffort = "high"
        XCTAssertNotNil(check(constrained))
    }

    func testUnknownOutputCapabilityStaysUnknownWhileApplicationDefaultIsBounded() {
        for provider in ["openai-api", "anthropic-api", "ollama"] {
            let unknown = MinutesModelFixtures.model(provider: provider, maxOutputTokens: nil)
            XCTAssertNil(unknown.maxOutputTokens)
            XCTAssertEqual(MinutesModelForm.defaultOutputLimit(unknown), 4096)
            XCTAssertEqual(MinutesModelForm.defaultOutputLimit(
                MinutesModelFixtures.model(provider: provider, maxOutputTokens: 2048)), 2048)
            let unverified = MinutesModelFixtures.model(availability: .unverified, provider: provider, maxOutputTokens: nil)
            XCTAssertFalse(MinutesModelForm.isTextMinutesModel(unverified, provider: provider))
            XCTAssertNotNil(MinutesModelForm.modelUnavailableReason(unverified, provider: provider))
        }
    }

    func testProviderAndModelChangesClearBothParametersButNeverChangePolicy() {
        var form = ProcessingConfigurationForm(config: MeetingConfig(
            externalSendPolicy: "local_only", minutesModel: MinutesModelSelection(
                provider: "openai-api", connectionID: MinutesModelFixtures.connectionID,
                connectionRevision: 1, modelID: "fixture-model", reasoningEffort: "high", maxTokens: 2048)))
        form.minutesModel.selectProvider("anthropic-api")
        XCTAssertEqual(form.minutesModel.provider, "anthropic-api")
        XCTAssertTrue(form.minutesModel.connectionID.isEmpty)
        XCTAssertTrue(form.minutesModel.reasoningEffort.isEmpty)
        XCTAssertTrue(form.minutesModel.maxTokensText.isEmpty)
        XCTAssertEqual(form.externalSendPolicy, "local_only")
        form.minutesModel.selectProvider("claude-agent-sdk")
        XCTAssertEqual(form.minutesModel.provider, "")
        XCTAssertNil(form.minutesModel.selection)
    }

    func testAPIRequiresSavedKeyAndRuntimeBeforeSelectingMinutes() {
        let connection = MinutesModelFixtures.connection(providerID: .openaiAPI)
        XCTAssertNil(MinutesModelForm.connectionUnavailableReason(
            connection, providers: [ProviderSettingsFixtures.provider(providerID: .openaiAPI)]))
        XCTAssertNotNil(MinutesModelForm.connectionUnavailableReason(connection, providers: []))
        XCTAssertNotNil(MinutesModelForm.connectionUnavailableReason(
            connection, providers: [ProviderSettingsFixtures.provider(providerID: .openaiAPI, state: .notPrepared)]))
        XCTAssertNotNil(MinutesModelForm.connectionUnavailableReason(
            ProviderSettingsFixtures.changed(connection, credential: false)))
    }

    func testMinutesConnectionsRequireVerifiedDestinationBeforeSelection() {
        for (provider, endpoint) in [
            (ProviderID.openaiAPI, "https://proxy.example.invalid"),
            (.anthropicAPI, "https://api.openai.com"),
            (.codexAppServer, "https://api.openai.com"),
            (.ollama, "http://localhost:11434"),
            (.ollama, "https://remote.example.invalid"),
        ] {
            let connection = ProviderConnection(
                connectionID: MinutesModelFixtures.connectionID, revision: 1, providerID: provider, displayName: "Fixture",
                enabled: true, endpoint: endpoint, authMethod: provider.supportedAuthMethod,
                credentialPresent: provider != .ollama, authState: .authenticated, catalogState: .ready,
                checkedAt: nil, activeAuth: nil, lastGenerationState: .never)
            XCTAssertNotNil(MinutesModelForm.connectionUnavailableReason(connection), endpoint)
        }
        XCTAssertNil(MinutesModelForm.connectionUnavailableReason(
            MinutesModelFixtures.connection(providerID: .ollama)))
    }

    func testVerifiedShutdownDateUsesUTCDateWithoutInventingTimeAndRemainsOptional() throws {
        let model = MinutesModelFixtures.model(provider: "openai-api", availabilityExpiresOn: "2026-08-29")
        let formatter = ISO8601DateFormatter()
        XCTAssertFalse(model.isAvailabilityExpired(at: try XCTUnwrap(formatter.date(from: "2026-08-28T23:59:59Z"))))
        XCTAssertTrue(model.isAvailabilityExpired(at: try XCTUnwrap(formatter.date(from: "2026-08-29T00:00:00Z"))))
        XCTAssertNil(MinutesModelFixtures.model().availabilityExpiresOn)
        XCTAssertFalse(MinutesModelFixtures.model().isAvailabilityExpired(at: Date.distantFuture))
        XCTAssertNotNil(MinutesModelForm.modelUnavailableReason(
            MinutesModelFixtures.model(provider: "openai-api", availabilityExpiresOn: "2000-01-01"), provider: "openai-api"))
        XCTAssertTrue(MinutesModelFixtures.model(availabilityExpiresOn: "2026-02-30").availabilityExpired)
    }

    func testServerCapabilitiesGateModelProvidersWithoutEnablingSDKGeneration() throws {
        var capabilities = ServerCapabilities(
            recording: false, transports: [], transcriptionEngines: [], diarizationEngines: [],
            llmProviders: ["claude-agent-sdk", "anthropic-api", "ollama"], exportDestinations: [],
            workflow: ProviderWorkflowCapabilities(
                providerConnections: true, providerModels: true, stageModelSelection: true, ensembleGeneration: false))
        XCTAssertEqual(capabilities.supportedMinutesModelProviders(contractVersion: "3.0.0"), ["codex-app-server"])
        XCTAssertTrue(capabilities.supportedMinutesModelProviders(contractVersion: "2.0.0").isEmpty)
        XCTAssertTrue(capabilities.supportedMinutesModelProviders(contractVersion: "4.0.0").isEmpty)
        capabilities.minutesModelProviders = []
        XCTAssertTrue(capabilities.supportedMinutesModelProviders(contractVersion: "4.0.0").isEmpty)
        capabilities.minutesModelProviders = ["openai-api", "ollama", "claude-agent-sdk", "unknown"]
        XCTAssertEqual(capabilities.supportedMinutesModelProviders(contractVersion: "4.0.0"), ["openai-api", "ollama"])
        XCTAssertEqual(capabilities.supportedMinutesModelProviders(contractVersion: "3.0.0"), ["codex-app-server"])
        XCTAssertEqual(capabilities.supportedMinutesModelProviders(contractVersion: "5.0.0"), ["openai-api", "ollama"])
        XCTAssertTrue(capabilities.supportedMinutesModelProviders(contractVersion: "6.0.0").isEmpty)
        capabilities.workflow?.stageModelSelection = false
        XCTAssertTrue(capabilities.supportedMinutesModelProviders(contractVersion: "4.0.0").isEmpty)
        let form = ProcessingConfigurationForm(config: MeetingConfig(minutesModel: MinutesModelSelection(
            provider: "openai-api", connectionID: MinutesModelFixtures.connectionID,
            connectionRevision: 1, modelID: "fixture-model")))
        XCTAssertThrowsError(try form.makeUpdate(supportedProviders: ["codex-app-server"]))
    }

    private func validate(_ form: ProcessingConfigurationForm) -> String? {
        form.minutesModel.validationMessage(
            connections: [MinutesModelFixtures.connection()], catalog: MinutesModelFixtures.catalog(),
            externalSendPolicy: form.effectiveExternalSendPolicy)
    }
}
