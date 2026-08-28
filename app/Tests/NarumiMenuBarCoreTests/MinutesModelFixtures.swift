import Foundation
@testable import NarumiMenuBarCore

enum MinutesModelFixtures {
    static let connectionID = ProviderSettingsFixtures.connectionID
    static let modelID = "fixture-codex-model"

    static func connection(
        revision: Int = 1, enabled: Bool = true, authState: ProviderAuthState = .authenticated
    ) -> ProviderConnection {
        ProviderSettingsFixtures.connection(
            revision: revision, providerID: .codexAppServer, name: "Minutes Codex", enabled: enabled,
            authState: authState, catalogState: .ready)
    }

    static func model(
        id: String = modelID, availability: ProviderAvailability = .available,
        inputs: [ProviderModality] = [.text], outputs: [ProviderModality] = [.text],
        billing: ProviderBillingKind = .subscription, required: [String] = []
    ) -> ProviderModelDescriptor {
        ProviderModelDescriptor(
            modelID: id, displayName: "Fixture Codex model", resolvedRevision: "fixture-revision",
            inputModalities: inputs, outputModalities: outputs, roles: [.llm], timestampSupport: .none,
            contextWindow: 100_000, maxOutputTokens: 10_000,
            parameterSchema: ProviderParameterSchema(properties: [
                "reasoning_effort": ProviderModelParameter(
                    type: .string, enumValues: [.string("low"), .string("high")], defaultValue: .string("low"))
            ], required: required),
            availability: availability, reason: nil, source: .runtime,
            fetchedAt: ProviderSettingsFixtures.timestamp,
            billing: ProviderModelBilling(
                kind: billing, inputUSDPerMillionTokens: nil, outputUSDPerMillionTokens: nil,
                audioUSDPerMinute: nil, fetchedAt: nil))
    }

    static func catalog(
        revision: Int = 1, models: [ProviderModelDescriptor] = [model()],
        state: ProviderCatalogState = .ready, cursor: String? = nil
    ) -> ListProviderModelsResponse {
        ListProviderModelsResponse(
            connectionID: connectionID, connectionRevision: revision, models: models,
            nextCursor: cursor, catalogState: state, fetchedAt: ProviderSettingsFixtures.timestamp)
    }

    static func selection(revision: Int = 1, cacheEpoch: Int = 0) -> CodexMinutesSelection {
        CodexMinutesSelection(
            connectionID: connectionID, connectionRevision: revision, modelID: modelID,
            reasoningEffort: "high", cacheEpoch: cacheEpoch)
    }

    static func config(policy: String = "subscription_ok", cacheEpoch: Int = 0) -> MeetingConfig {
        MeetingConfig(
            transcriptionEngine: "auto", diarizationEngine: "none", llmProvider: "none",
            externalSendPolicy: policy, language: "ja", selfName: "Fixture speaker", vocabHints: ["Fixture"],
            minutesModel: selection(cacheEpoch: cacheEpoch))
    }

    static func detail(config: MeetingConfig = config()) throws -> MeetingDetail {
        let base = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        let data = try Data(contentsOf: base.appendingPathComponent("contracts/tools/get_meeting.json"))
        let root = try JSONSerialization.jsonObject(with: data) as! [String: Any]
        let outputs = (root["examples"] as! [String: Any])["output"] as! [[String: Any]]
        var detail = try JSONDecoder().decode(MeetingDetail.self, from: JSONSerialization.data(withJSONObject: outputs[0]))
        detail.config = config
        return detail
    }
}
