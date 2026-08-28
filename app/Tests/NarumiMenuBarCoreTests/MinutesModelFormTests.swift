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
        form.minutesModel.mode = .codex
        XCTAssertThrowsError(try form.makeUpdate())
        form.minutesModel.selectConnection(MinutesModelFixtures.connection())
        XCTAssertThrowsError(try form.makeUpdate())
        form.minutesModel.selectModel(MinutesModelFixtures.model())
        XCTAssertNotNil(try form.makeUpdate().config.minutesModel)
    }

    func testSelectionDoesNotRaisePolicyAutomatically() {
        var form = ProcessingConfigurationForm()
        form.minutesModel.mode = .codex
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
            CodexMinutesSelection.self, from: JSONSerialization.data(withJSONObject: minimal))
        XCTAssertEqual(selection.cacheEpoch, 0)
        XCTAssertNil(selection.parameters.reasoningEffort)
        for (key, value) in [
            ("provider", "anthropic-api" as Any), ("connection_revision", 0), ("cache_epoch", -1),
            ("model_id", ""), ("parameters", NSNull()),
            ("parameters", ["reasoning_effort": "wrong value"]),
            ("parameters", ["reasoning_effort": NSNull()]),
            ("parameters", ["command": "fixture"]), ("endpoint", "https://example.invalid"),
        ] {
            var invalid = minimal
            invalid[key] = value
            XCTAssertThrowsError(try JSONDecoder().decode(
                CodexMinutesSelection.self, from: JSONSerialization.data(withJSONObject: invalid)))
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

    private func validate(_ form: ProcessingConfigurationForm) -> String? {
        form.minutesModel.validationMessage(
            connections: [MinutesModelFixtures.connection()], catalog: MinutesModelFixtures.catalog(),
            externalSendPolicy: form.effectiveExternalSendPolicy)
    }
}
