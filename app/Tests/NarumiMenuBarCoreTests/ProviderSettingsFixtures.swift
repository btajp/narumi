import Foundation
@testable import NarumiMenuBarCore

enum ProviderSettingsFixtures {
    static let connectionID = "conn-0123456789ab"
    static let operationID = "auth-0123456789ab"
    static let instanceID = "00000000-0000-4000-8000-000000000001"
    static let jobID = "job-0123456789ab"
    static let timestamp = "2026-08-28T09:00:00Z"
    static let userCode = "ABCD-EFGH"

    static func connection(
        connectionID: String = ProviderSettingsFixtures.connectionID,
        revision: Int = 1, providerID: ProviderID = .anthropicAPI, name: String = "Test connection",
        enabled: Bool = true, credential: Bool = true, authState: ProviderAuthState = .unverified,
        catalogState: ProviderCatalogState = .unfetched, activeAuth: ProviderActiveAuth? = nil,
        generation: ProviderGenerationState = .never, endpoint: String? = nil,
        authMethod: ProviderAuthMethod? = nil, apiSurface: ProviderAPISurface? = nil,
        chatMaxTokensField: ProviderChatMaxTokensField? = nil
    ) -> ProviderConnection {
        let method = authMethod ?? providerID.defaultAuthMethod
        let destination = endpoint ?? (providerID == .openAICompatibleAPI
            ? "https://compatible.example.invalid/v1"
            : ProviderConnectionSettings(providerID: providerID).normalizedEndpoint)
        return ProviderConnection(
            connectionID: connectionID, revision: revision, providerID: providerID, displayName: name,
            enabled: enabled, endpoint: destination, authMethod: method,
            apiSurface: apiSurface ?? (providerID == .openAICompatibleAPI ? .responses : nil),
            chatMaxTokensField: chatMaxTokensField,
            credentialPresent: method == .none ? false : credential,
            authState: authState, catalogState: catalogState, checkedAt: nil,
            activeAuth: activeAuth, lastGenerationState: generation)
    }

    static func changed(
        _ connection: ProviderConnection, revision: Int? = nil, name: String? = nil,
        enabled: Bool? = nil, credential: Bool? = nil, authState: ProviderAuthState? = nil,
        catalogState: ProviderCatalogState? = nil, activeAuth: ProviderActiveAuth? = nil
    ) -> ProviderConnection {
        ProviderConnection(
            connectionID: connection.connectionID, revision: revision ?? connection.revision,
            providerID: connection.providerID, displayName: name ?? connection.displayName,
            enabled: enabled ?? connection.enabled, endpoint: connection.endpoint, authMethod: connection.authMethod,
            apiSurface: connection.apiSurface, chatMaxTokensField: connection.chatMaxTokensField,
            credentialPresent: credential ?? connection.credentialPresent,
            authState: authState ?? connection.authState, catalogState: catalogState ?? connection.catalogState,
            checkedAt: timestamp, activeAuth: activeAuth, lastGenerationState: connection.lastGenerationState)
    }

    static func resource(
        version: String? = nil, source: ProviderRuntimeResourceSource = .installed,
        sha256: String? = nil
    ) -> ProviderRuntimeResource {
        ProviderRuntimeResource(
            resourceID: "fixture-runtime", displayName: "Installed fixture runtime", kind: .runtime,
            version: version, source: source, downloadHost: nil, sha256: sha256,
            license: "Fixture license")
    }

    static func provider(
        providerID: ProviderID = .anthropicAPI, state: ProviderRuntimeState = .ready,
        activeSetup: ProviderSetupOperation? = nil, lastSetup: ProviderSetupOperation? = nil,
        resource: ProviderRuntimeResource = ProviderSettingsFixtures.resource()
    ) -> ProviderDescriptor {
        ProviderDescriptor(
            providerID: providerID, displayName: ProviderDisplay.name(providerID), roles: [.llm],
            authMethods: providerID.supportedAuthMethods, availability: .unverified,
            reason: "adapter_capability_verification_required",
            runtime: ProviderRuntime(
                state: state, version: nil, catalogRevision: "fixture-v1", resources: [resource],
                activeSetup: activeSetup, lastSetup: lastSetup))
    }

    static func auth(
        requestID: String = "original-start", state: ProviderAuthOperationState = .pending,
        instanceID: String = ProviderSettingsFixtures.instanceID, action: ProviderAuthAction = .start,
        connectionID: String = ProviderSettingsFixtures.connectionID, connectionRevision: Int = 1,
        authorizationURL: ProviderAuthorizationURL? = nil, userCode: ProviderUserCode? = nil,
        reason: String? = nil
    ) -> ProviderAuthOperation {
        ProviderAuthOperation(
            operationID: operationID, connectionID: connectionID, connectionRevision: connectionRevision,
            serverInstanceID: instanceID, startRequestID: requestID, action: action, state: state,
            authorizationURL: authorizationURL,
            userCode: authorizationURL == nil ? userCode : (userCode ?? ProviderUserCode(Self.userCode)),
            reason: reason ?? (state == .unknown ? "authentication_operation_interrupted" : nil),
            createdAt: timestamp, updatedAt: timestamp)
    }

    static func authorizationURL() -> String { "https://auth.openai.com/codex/device" }

    static func activeAuth(requestID: String = "original-start", state: ProviderAuthOperationState = .pending) -> ProviderActiveAuth {
        ProviderActiveAuth(operationID: operationID, startRequestID: requestID, serverInstanceID: instanceID, state: state)
    }

    static func setup(requestID: String = "original-setup", state: ProviderSetupState = .running) -> ProviderSetupOperation {
        ProviderSetupOperation(jobID: jobID, startRequestID: requestID, resourceID: resource().resourceID, state: state)
    }

    static func job(state: String = "running", kind: String = "provider_setup") -> Job {
        Job(jobID: jobID, meetingID: nil, kind: kind, status: state, progress: nil, result: nil, error: nil,
            createdAt: timestamp, updatedAt: timestamp)
    }

    static func model() -> ProviderModelDescriptor {
        ProviderModelDescriptor(
            modelID: "fixture-model", displayName: "Fixture model", resolvedRevision: nil,
            inputModalities: [.text], outputModalities: [.text], roles: [.llm], timestampSupport: .none,
            contextWindow: nil, maxOutputTokens: nil, parameterSchema: ProviderParameterSchema(),
            availability: .unverified, reason: "model_capabilities_unavailable", source: .providerAPI,
            fetchedAt: timestamp, billing: ProviderModelBilling(
                kind: .api, inputUSDPerMillionTokens: nil, outputUSDPerMillionTokens: nil,
                audioUSDPerMinute: nil, fetchedAt: nil))
    }
}
