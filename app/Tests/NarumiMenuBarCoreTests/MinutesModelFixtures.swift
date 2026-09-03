import Foundation
@testable import NarumiMenuBarCore

enum MinutesModelFixtures {
    static let connectionID = ProviderSettingsFixtures.connectionID
    static let modelID = "fixture-codex-model"

    static func connection(
        revision: Int = 1, enabled: Bool = true, authState: ProviderAuthState = .authenticated,
        providerID: ProviderID = .codexAppServer
    ) -> ProviderConnection {
        ProviderSettingsFixtures.connection(
            revision: revision, providerID: providerID, name: "Minutes fixture", enabled: enabled,
            authState: authState, catalogState: .ready)
    }

    static func model(
        id: String = modelID, availability: ProviderAvailability = .available,
        inputs: [ProviderModality] = [.text], outputs: [ProviderModality] = [.text],
        roles: [ProviderRole] = [.llm],
        billing: ProviderBillingKind? = nil, required: [String] = [], provider: String = "codex-app-server",
        parameterSchema: ProviderParameterSchema? = nil, maxOutputTokens: Int? = 10_000,
        contextWindow: Int? = 100_000, availabilityExpiresOn: String? = nil, reason: String? = nil,
        source: ProviderModelSource = .runtime
    ) -> ProviderModelDescriptor {
        var properties: [String: ProviderModelParameter] = [:]
        if ["codex-app-server", "openai-api"].contains(provider) {
            properties["reasoning_effort"] = ProviderModelParameter(
                type: .string, enumValues: [.string("low"), .string("high")], defaultValue: .string("low"))
        }
        if !["codex-app-server", "claude-agent-sdk"].contains(provider) {
            properties["max_tokens"] = ProviderModelParameter(type: .integer, minimum: 1, maximum: 32768)
        }
        let billingKind = billing ?? (provider == "codex-app-server" ? .subscription : (provider == "ollama" ? .local : .api))
        return ProviderModelDescriptor(
            modelID: id, displayName: "Fixture Codex model", resolvedRevision: "fixture-revision",
            inputModalities: inputs, outputModalities: outputs, roles: roles, timestampSupport: .none,
            contextWindow: contextWindow, maxOutputTokens: maxOutputTokens,
            parameterSchema: parameterSchema ?? ProviderParameterSchema(properties: properties, required: required),
            availability: availability, reason: reason, source: source,
            fetchedAt: ProviderSettingsFixtures.timestamp,
            billing: ProviderModelBilling(
                kind: billingKind, inputUSDPerMillionTokens: nil, outputUSDPerMillionTokens: nil,
                audioUSDPerMinute: nil, fetchedAt: nil), availabilityExpiresOn: availabilityExpiresOn)
    }

    static func catalog(
        revision: Int = 1, models: [ProviderModelDescriptor] = [model()],
        state: ProviderCatalogState = .ready, cursor: String? = nil
    ) -> ListProviderModelsResponse {
        ListProviderModelsResponse(
            connectionID: connectionID, connectionRevision: revision, models: models,
            nextCursor: cursor, catalogState: state, fetchedAt: ProviderSettingsFixtures.timestamp)
    }

    static func selection(revision: Int = 1, cacheEpoch: Int = 0) -> MinutesModelSelection {
        MinutesModelSelection(
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
