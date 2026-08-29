import XCTest
@testable import NarumiMenuBarCore

final class MinutesEnsembleCoreTests: XCTestCase {
    private func selection(provider: String = "openai-api") -> MinutesModelSelection {
        MinutesModelSelection(
            provider: provider, connectionID: "conn-111122223333", connectionRevision: 2,
            modelID: "fixture-model", reasoningEffort: provider == "openai-api" ? "high" : nil,
            maxTokens: provider == "codex-app-server" ? nil : 4096)
    }

    private func ensemble() -> MinutesEnsembleSelection {
        let model = selection()
        return MinutesEnsembleSelection(generators: [
            .init(id: "gen-11111111111111111111111111111111", label: "案1", selection: model),
            .init(id: "gen-22222222222222222222222222222222", label: "案2", selection: model),
        ], synthesizer: model)
    }

    func testSelectionIsClosedAndRequiresTwoToFourUniqueStableIDs() throws {
        let value = ensemble()
        XCTAssertTrue(value.isWellFormed)
        XCTAssertEqual(try JSONDecoder().decode(
            MinutesEnsembleSelection.self, from: JSONEncoder().encode(value)), value)
        var object = try XCTUnwrap(JSONSerialization.jsonObject(with: JSONEncoder().encode(value)) as? [String: Any])
        object["secret"] = "must not decode"
        XCTAssertThrowsError(try JSONDecoder().decode(
            MinutesEnsembleSelection.self, from: JSONSerialization.data(withJSONObject: object)))
        let duplicate = MinutesEnsembleSelection(
            generators: [value.generators[0], value.generators[0]], synthesizer: value.synthesizer)
        XCTAssertFalse(duplicate.isWellFormed)
        XCTAssertThrowsError(try JSONEncoder().encode(duplicate))
    }

    func testFormKeepsStableCardsAndEnforcesTwoToFour() {
        var form = MinutesEnsembleForm(selection: ensemble())
        XCTAssertEqual(form.selection, ensemble())
        XCTAssertFalse(form.removeGenerator(id: "missing"))
        XCTAssertFalse(form.removeGenerator(id: form.generators[0].id))
        XCTAssertTrue(form.addGenerator(id: "gen-33333333333333333333333333333333", label: "案3"))
        XCTAssertTrue(form.addGenerator(id: "gen-44444444444444444444444444444444", label: "案4"))
        XCTAssertFalse(form.addGenerator(id: "gen-55555555555555555555555555555555", label: "案5"))
        XCTAssertEqual(form.generators.map(\.label), ["案1", "案2", "案3", "案4"])
    }

    func testSelectingEnsembleActivatesEveryParticipantEditor() {
        var form = ProcessingConfigurationForm()
        XCTAssertEqual(form.minutesGenerationMode, .legacy)
        form.selectMinutesGenerationMode(.ensemble)
        XCTAssertEqual(form.minutesGenerationMode, .ensemble)
        XCTAssertTrue(form.minutesEnsemble.generators.allSatisfy { $0.model.mode == .selected })
        XCTAssertEqual(form.minutesEnsemble.synthesizer.mode, .selected)
        XCTAssertNil(form.minutesEnsemble.selection)
    }

    func testConfigurationSwitchWritesBothSidesOfExclusiveOverride() throws {
        var form = ProcessingConfigurationForm(config: MeetingConfig(
            externalSendPolicy: "api_ok", minutesModel: selection()))
        form.minutesEnsemble = MinutesEnsembleForm(selection: ensemble())
        form.selectMinutesGenerationMode(.ensemble)
        let update = try form.makeUpdate(supportsMinutesEnsembleWire: true)
        let object = try XCTUnwrap(JSONSerialization.jsonObject(
            with: JSONEncoder().encode(update)) as? [String: Any])
        XCTAssertTrue(object["minutes_model"] is NSNull)
        XCTAssertNotNil(object["minutes_ensemble"])
        let effective = update.applying(to: form.originalConfig)
        XCTAssertNil(effective.minutesModel)
        XCTAssertEqual(effective.minutesEnsemble, ensemble())

        var back = ProcessingConfigurationForm(config: effective)
        back.minutesModel = MinutesModelForm(selection: selection())
        back.selectMinutesGenerationMode(.single)
        let single = try back.makeUpdate(supportsMinutesEnsembleWire: true)
        let singleObject = try XCTUnwrap(JSONSerialization.jsonObject(
            with: JSONEncoder().encode(single)) as? [String: Any])
        XCTAssertTrue(singleObject["minutes_ensemble"] is NSNull)
        XCTAssertNotNil(singleObject["minutes_model"])
    }

    func testContractFiveUpdateNeverWritesOrClearsEnsemble() throws {
        let original = MeetingConfig(externalSendPolicy: "api_ok", minutesEnsemble: ensemble())
        var form = ProcessingConfigurationForm(config: original)
        XCTAssertThrowsError(try form.makeUpdate(supportsMinutesEnsembleWire: false))
        form.selectMinutesGenerationMode(.legacy)
        let update = try form.makeUpdate(supportsMinutesEnsembleWire: false)
        let object = try XCTUnwrap(JSONSerialization.jsonObject(
            with: JSONEncoder().encode(update)) as? [String: Any])
        XCTAssertNil(object["minutes_ensemble"])
        XCTAssertEqual(update.applying(to: original).minutesEnsemble, original.minutesEnsemble)
    }

    func testMeetingConfigRejectsBothOverrides() throws {
        let invalid = MeetingConfig(
            externalSendPolicy: "api_ok", minutesModel: selection(), minutesEnsemble: ensemble())
        XCTAssertThrowsError(try JSONEncoder().encode(invalid))
        let model = try XCTUnwrap(JSONSerialization.jsonObject(with: JSONEncoder().encode(selection())) as? [String: Any])
        let ensembleObject = try XCTUnwrap(JSONSerialization.jsonObject(with: JSONEncoder().encode(ensemble())) as? [String: Any])
        XCTAssertThrowsError(try JSONDecoder().decode(MeetingConfig.self, from: JSONSerialization.data(
            withJSONObject: ["minutes_model": model, "minutes_ensemble": ensembleObject])))
    }

    func testContractSixCapabilityRequiresExactAdvertisedLimits() throws {
        let workflow = ProviderWorkflowCapabilities(
            providerConnections: true, providerModels: true, stageModelSelection: true, ensembleGeneration: true)
        var capabilities = ServerCapabilities(
            recording: true, transports: ["streamable-http"], transcriptionEngines: [],
            diarizationEngines: [], llmProviders: [], exportDestinations: [], workflow: workflow,
            minutesModelProviders: ["openai-api"], minutesEnsembleLimits: MinutesEnsembleLimits())
        XCTAssertTrue(capabilities.supportsMinutesEnsemble(contractVersion: "6.0.0"))
        XCTAssertFalse(capabilities.supportsMinutesEnsemble(contractVersion: "5.0.0"))
        capabilities.minutesEnsembleLimits = nil
        XCTAssertTrue(capabilities.supportsMinutesEnsembleWire(contractVersion: "6.0.0"))
        XCTAssertFalse(capabilities.supportsMinutesEnsemble(contractVersion: "6.0.0"))

        let legacy = try JSONDecoder().decode(ProviderWorkflowCapabilities.self, from: Data(
            #"{"provider_connections":true,"provider_models":true,"stage_model_selection":true}"#.utf8))
        XCTAssertFalse(legacy.ensembleGeneration)
    }

    func testContractSixCanClearSavedEnsembleWhileExecutionIsUnavailable() throws {
        let original = MeetingConfig(externalSendPolicy: "api_ok", minutesEnsemble: ensemble())
        var form = ProcessingConfigurationForm(config: original)
        form.selectMinutesGenerationMode(.legacy)
        let update = try form.makeUpdate(
            supportsMinutesEnsembleWire: true, canExecuteMinutesEnsemble: false)
        let object = try XCTUnwrap(JSONSerialization.jsonObject(
            with: JSONEncoder().encode(update)) as? [String: Any])
        XCTAssertTrue(object["minutes_ensemble"] is NSNull)
        XCTAssertNil(update.applying(to: original).minutesEnsemble)
    }

    func testEnsembleExecutionAvailabilityExplainsCapabilityFailures() {
        let readyWorkflow = ProviderWorkflowCapabilities(
            providerConnections: true, providerModels: true,
            stageModelSelection: true, ensembleGeneration: true)
        var capabilities = ServerCapabilities(
            recording: true, transports: ["streamable-http"], transcriptionEngines: [],
            diarizationEngines: [], llmProviders: [], exportDestinations: [], workflow: readyWorkflow,
            minutesModelProviders: ["openai-api"], minutesEnsembleLimits: MinutesEnsembleLimits())
        XCTAssertNil(MinutesEnsembleExecutionAvailability.unavailableReason(
            capabilities: capabilities, contractVersion: "6.0.0", supportedProviders: ["openai-api"]))

        capabilities.workflow = ProviderWorkflowCapabilities(
            providerConnections: true, providerModels: true,
            stageModelSelection: true, ensembleGeneration: false)
        let stopped = MinutesEnsembleExecutionAvailability.unavailableReason(
            capabilities: capabilities, contractVersion: "6.0.0", supportedProviders: ["openai-api"])
        XCTAssertTrue(stopped?.contains("保存済み設定は確認できます") == true)
        XCTAssertTrue(stopped?.contains("新しい実行には使えません") == true)

        capabilities.workflow = readyWorkflow
        capabilities.minutesEnsembleLimits = nil
        XCTAssertTrue(MinutesEnsembleExecutionAvailability.unavailableReason(
            capabilities: capabilities, contractVersion: "6.0.0", supportedProviders: ["openai-api"]
        )?.contains("保存済み設定は確認できます") == true)
        capabilities.minutesEnsembleLimits = MinutesEnsembleLimits()
        XCTAssertTrue(MinutesEnsembleExecutionAvailability.unavailableReason(
            capabilities: capabilities, contractVersion: "6.0.0",
            supportedProviders: [])?.contains("プロバイダ") == true)
        XCTAssertTrue(MinutesEnsembleExecutionAvailability.unavailableReason(
            capabilities: capabilities, contractVersion: "5.0.0",
            supportedProviders: ["openai-api"])?.contains("この契約") == true)
    }

    func testParticipantDisclosureUsesProviderDestinationAndBillingBoundary() throws {
        let codex = try XCTUnwrap(MinutesParticipantDisclosure.make(provider: "codex-app-server"))
        XCTAssertEqual(codex.destination, "OpenAI（chatgpt.com）")
        XCTAssertEqual(codex.billing, .subscription)
        let openAI = try XCTUnwrap(MinutesParticipantDisclosure.make(provider: "openai-api"))
        XCTAssertEqual(openAI.destination, "OpenAI API（api.openai.com）")
        XCTAssertEqual(openAI.billing, .api)
        let anthropic = try XCTUnwrap(MinutesParticipantDisclosure.make(provider: "anthropic-api"))
        XCTAssertEqual(anthropic.destination, "Anthropic API（api.anthropic.com）")
        XCTAssertEqual(anthropic.billing, .api)
        let ollama = try XCTUnwrap(MinutesParticipantDisclosure.make(
            provider: "ollama", endpoint: "http://127.0.0.1:11434"))
        XCTAssertEqual(ollama.destination, "この Mac の Ollama（http://127.0.0.1:11434）")
        XCTAssertEqual(ollama.billing, .local)
        XCTAssertNil(MinutesParticipantDisclosure.make(provider: "claude-agent-sdk"))
    }
}
