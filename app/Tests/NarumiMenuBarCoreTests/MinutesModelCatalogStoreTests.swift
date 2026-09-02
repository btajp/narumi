import XCTest
@testable import NarumiMenuBarCore

@MainActor
final class MinutesModelCatalogStoreTests: XCTestCase {
    func testOpeningEditorOnlyReadsCachedMetadata() async {
        let client = FakeMinutesCatalogClient()
        let store = MinutesModelCatalogStore(client: client, supportedProviders: ["codex-app-server"])
        await store.loadCachedCatalog(connectionID: MinutesModelFixtures.connectionID)
        let requests = await client.requests
        XCTAssertEqual(requests.count, 1)
        XCTAssertFalse(requests[0].refresh)
        XCTAssertEqual(requests[0].role, .llm)
        XCTAssertEqual(store.connections(for: "codex-app-server").count, 1)
        XCTAssertNil(store.validationMessage(for: ProcessingConfigurationForm(config: MinutesModelFixtures.config())))
        XCTAssertFalse(store.isLoading)
    }

    func testOpeningEmptyDraftDoesNotFetchAnyModelsOrSelectAConnection() async {
        let client = FakeMinutesCatalogClient()
        let store = MinutesModelCatalogStore(client: client, supportedProviders: ["codex-app-server"])
        await store.loadCachedCatalog(connectionID: "")
        let requests = await client.requests
        XCTAssertTrue(requests.isEmpty)
        XCTAssertEqual(store.connections(for: "codex-app-server").count, 1)
        XCTAssertTrue(store.catalogs.isEmpty)
    }

    func testOnlyExplicitRefreshRequestsAnUpstreamCatalog() async {
        let client = FakeMinutesCatalogClient()
        let store = MinutesModelCatalogStore(client: client, supportedProviders: ["codex-app-server"])
        await store.loadCachedCatalog(connectionID: MinutesModelFixtures.connectionID)
        await store.refreshModels(connectionID: MinutesModelFixtures.connectionID)
        let requests = await client.requests
        XCTAssertEqual(requests.map(\.refresh), [false, true])
        XCTAssertNil(store.errorMessage)
    }

    func testRevisionConflictDiscardsModelsInsteadOfAdoptingNewRevision() async {
        let client = FakeMinutesCatalogClient(pages: ["first": MinutesModelFixtures.catalog(revision: 2)])
        let store = MinutesModelCatalogStore(client: client, supportedProviders: ["codex-app-server"])
        await store.loadCachedCatalog(connectionID: MinutesModelFixtures.connectionID)
        XCTAssertNil(store.catalogs[MinutesModelFixtures.connectionID])
        XCTAssertTrue(store.errorMessage?.contains("configuration_conflict") == true)
        XCTAssertNotNil(store.validationMessage(for: ProcessingConfigurationForm(config: MinutesModelFixtures.config())))
    }

    func testConnectionChangeInvalidatesOldCatalogAndLeavesSavedSelectionPinned() async {
        let client = FakeMinutesCatalogClient()
        let store = MinutesModelCatalogStore(client: client, supportedProviders: ["codex-app-server"])
        let form = ProcessingConfigurationForm(config: MinutesModelFixtures.config())
        await store.loadCachedCatalog(connectionID: MinutesModelFixtures.connectionID)
        await client.replace(
            connections: [MinutesModelFixtures.connection(revision: 2)],
            pages: ["first": MinutesModelFixtures.catalog(revision: 2)])
        await store.loadCachedCatalog(connectionID: MinutesModelFixtures.connectionID)
        XCTAssertEqual(form.minutesModel.connectionRevision, 1)
        XCTAssertNotNil(store.validationMessage(for: form))
        XCTAssertEqual(store.catalogs[MinutesModelFixtures.connectionID]?.connectionRevision, 2)
    }

    func testSavedSelectionBeyondTheFirstPageCanBeReloadedAndSaved() async {
        let client = FakeMinutesCatalogClient(pages: [
            "first": MinutesModelFixtures.catalog(models: [MinutesModelFixtures.model(id: "first-model")], cursor: "next-page"),
            "next-page": MinutesModelFixtures.catalog(),
        ])
        let store = MinutesModelCatalogStore(client: client, supportedProviders: ["codex-app-server"])
        await store.loadCachedCatalog(
            connectionID: MinutesModelFixtures.connectionID, selectedModelID: MinutesModelFixtures.modelID)
        let requests = await client.requests
        XCTAssertEqual(requests.map(\.cursor), [nil, "next-page"])
        XCTAssertTrue(requests.allSatisfy { !$0.refresh })
        XCTAssertEqual(store.catalogs[MinutesModelFixtures.connectionID]?.models.count, 2)
        XCTAssertNil(store.validationMessage(for: ProcessingConfigurationForm(config: MinutesModelFixtures.config())))
    }

    func testLoadingMoreOnlyReadsTheCachedPageAndDeduplicates() async {
        let client = FakeMinutesCatalogClient(pages: [
            "first": MinutesModelFixtures.catalog(cursor: "next-page"),
            "next-page": MinutesModelFixtures.catalog(
                models: [MinutesModelFixtures.model(), MinutesModelFixtures.model(id: "second-model")]),
        ])
        let store = MinutesModelCatalogStore(client: client, supportedProviders: ["codex-app-server"])
        await store.loadCachedCatalog(connectionID: MinutesModelFixtures.connectionID)
        await store.loadMoreModels(connectionID: MinutesModelFixtures.connectionID)
        XCTAssertEqual(store.catalogs[MinutesModelFixtures.connectionID]?.models.map(\.modelID), [MinutesModelFixtures.modelID, "second-model"])
        let requests = await client.requests
        XCTAssertTrue(requests.allSatisfy { !$0.refresh })
    }

    func testInvalidationDiscardsLateResultsAndBusyState() async {
        let client = FakeMinutesCatalogClient(holdModels: true)
        let store = MinutesModelCatalogStore(client: client, supportedProviders: ["codex-app-server"])
        let load = Task { await store.loadCachedCatalog(connectionID: MinutesModelFixtures.connectionID) }
        await client.waitForModelRequest()
        store.invalidate()
        await client.releaseModelRequest()
        await load.value
        XCTAssertTrue(store.connections.isEmpty)
        XCTAssertTrue(store.catalogs.isEmpty)
        XCTAssertFalse(store.isLoading)
    }

    func testCancelledReadDoesNotLeaveTheEditorBusy() async {
        let client = FakeMinutesCatalogClient(holdModels: true)
        let store = MinutesModelCatalogStore(client: client, supportedProviders: ["codex-app-server"])
        let load = Task { await store.loadCachedCatalog(connectionID: MinutesModelFixtures.connectionID) }
        await client.waitForModelRequest()
        load.cancel()
        await client.releaseModelRequest()
        await load.value
        XCTAssertFalse(store.isLoading)
        XCTAssertTrue(store.catalogs.isEmpty)
    }

    func testMetadataFailureClearsCatalogWithoutDisplayingUpstreamDetails() async {
        let client = FakeMinutesCatalogClient()
        let store = MinutesModelCatalogStore(client: client, supportedProviders: ["codex-app-server"])
        await store.loadCachedCatalog(connectionID: MinutesModelFixtures.connectionID)
        await client.failNextRequest()
        await store.refreshModels(connectionID: MinutesModelFixtures.connectionID)
        XCTAssertTrue(store.catalogs.isEmpty)
        XCTAssertFalse(store.errorMessage?.contains("fixture-sensitive-upstream-text") == true)
        XCTAssertTrue(store.errorMessage?.contains("internal") == true)
    }

    func testCapabilityMissingOrExplicitlyRemovedNeverFetchesAnotherProvider() async {
        let connection = MinutesModelFixtures.connection(providerID: .openaiAPI)
        let client = FakeMinutesCatalogClient(connections: [connection])
        let store = MinutesModelCatalogStore(client: client)
        await store.loadCachedCatalog(connectionID: connection.connectionID)
        await store.refreshModels(connectionID: connection.connectionID)
        let beforeAdvertising = await client.requests
        XCTAssertTrue(beforeAdvertising.isEmpty)
        XCTAssertTrue(store.connections(for: "openai-api").isEmpty)
        store.setSupportedProviders(["openai-api", "claude-agent-sdk"])
        XCTAssertEqual(store.supportedProviders, ["claude-agent-sdk", "openai-api"])
        await store.loadCachedCatalog(connectionID: connection.connectionID)
        let afterAdvertising = await client.requests
        XCTAssertEqual(afterAdvertising.count, 1)
        store.setSupportedProviders([])
        XCTAssertTrue(store.catalogs.isEmpty)
        await store.refreshModels(connectionID: connection.connectionID)
        let afterRemoval = await client.requests
        XCTAssertEqual(afterRemoval.count, 1)
    }

    func testAllSixProvidersUseExactConnectionAndOnlyExplicitCatalogRefresh() async {
        for name in MinutesModelSelection.providers {
            let providerID = ProviderID(rawValue: name)!
            let connection = MinutesModelFixtures.connection(providerID: providerID)
            let model = MinutesModelFixtures.model(provider: name)
            let client = FakeMinutesCatalogClient(
                connections: [connection], pages: ["first": MinutesModelFixtures.catalog(models: [model])])
            let store = MinutesModelCatalogStore(client: client, supportedProviders: [name])
            await store.loadCachedCatalog(connectionID: connection.connectionID, selectedModelID: model.modelID)
            let selection = MinutesModelSelection(
                provider: name, connectionID: connection.connectionID, connectionRevision: 1, modelID: model.modelID)
            let config = MeetingConfig(
                externalSendPolicy: name == "ollama" ? "local_only" : "api_ok", minutesModel: selection)
            XCTAssertNil(store.validationMessage(for: ProcessingConfigurationForm(config: config)), name)
            await store.refreshModels(connectionID: connection.connectionID)
            let requests = await client.requests
            XCTAssertEqual(requests.map(\.connectionID), [connection.connectionID, connection.connectionID], name)
            XCTAssertEqual(requests.map(\.refresh), [false, true], name)
        }
    }

    func testMissingOrRevokedVerificationCapabilityNeverSendsThePaidProbe() async {
        let connection = MinutesModelFixtures.connection(providerID: .openAICompatibleAPI)
        let candidate = MinutesModelFixtures.model(
            id: "compatible-candidate", availability: .unverified, inputs: [], outputs: [], roles: [],
            provider: "openai-compatible-api", parameterSchema: ProviderParameterSchema(),
            maxOutputTokens: nil, contextWindow: nil,
            reason: "adapter_capability_verification_required", source: .providerAPI)
        let client = FakeMinutesCatalogClient(
            connections: [connection], pages: ["first": MinutesModelFixtures.catalog(models: [candidate])])
        let store = MinutesModelCatalogStore(client: client, supportedProviders: ["openai-compatible-api"])
        await store.loadCachedCatalog(connectionID: connection.connectionID)

        XCTAssertFalse(store.isVerificationCandidate(
            connectionID: connection.connectionID, expectedRevision: connection.revision, model: candidate))
        await store.verifyModel(
            connectionID: connection.connectionID, expectedRevision: connection.revision,
            expectedModel: candidate, confirmation: .sendTestPromptAndMayCharge)
        let requestsBeforeEnable = await client.verificationRequests
        XCTAssertTrue(requestsBeforeEnable.isEmpty)

        store.setSupportedProviders(["openai-compatible-api"], modelVerificationSupported: true)
        XCTAssertTrue(store.isVerificationCandidate(
            connectionID: connection.connectionID, expectedRevision: connection.revision, model: candidate))
        store.setSupportedProviders(["openai-compatible-api"], modelVerificationSupported: false)
        XCTAssertFalse(store.isVerificationCandidate(
            connectionID: connection.connectionID, expectedRevision: connection.revision, model: candidate))
        let requestsAfterRevoke = await client.verificationRequests
        XCTAssertTrue(requestsAfterRevoke.isEmpty)
    }

    func testCompatibleModelVerificationIsExplicitSingleSendAndReloadsCachedCatalog() async {
        let connection = MinutesModelFixtures.connection(providerID: .openAICompatibleAPI)
        let candidate = MinutesModelFixtures.model(
            id: "compatible-candidate", availability: .unverified, inputs: [], outputs: [], roles: [],
            provider: "openai-compatible-api", parameterSchema: ProviderParameterSchema(),
            maxOutputTokens: nil, contextWindow: nil,
            reason: "adapter_capability_verification_required", source: .providerAPI)
        let client = FakeMinutesCatalogClient(
            connections: [connection], pages: ["first": MinutesModelFixtures.catalog(models: [candidate])])
        let store = MinutesModelCatalogStore(
            client: client, supportedProviders: ["openai-compatible-api"], modelVerificationSupported: true)
        await store.loadCachedCatalog(connectionID: connection.connectionID)
        XCTAssertEqual(store.catalogs[connection.connectionID]?.models.first?.availability, .unverified)

        await store.verifyModel(
            connectionID: connection.connectionID, expectedRevision: connection.revision, expectedModel: candidate,
            confirmation: .sendTestPromptAndMayCharge)

        let verificationRequests = await client.verificationRequests
        XCTAssertEqual(verificationRequests.count, 1)
        XCTAssertEqual(verificationRequests.first?.connectionID, connection.connectionID)
        XCTAssertEqual(verificationRequests.first?.expectedRevision, connection.revision)
        XCTAssertEqual(verificationRequests.first?.modelID, candidate.modelID)
        XCTAssertEqual(verificationRequests.first?.confirmation, .sendTestPromptAndMayCharge)
        XCTAssertEqual(store.catalogs[connection.connectionID]?.models.first {
            $0.modelID == candidate.modelID
        }?.availability, .available)
        XCTAssertNil(store.verificationNotice)
        let modelRequests = await client.requests
        XCTAssertEqual(modelRequests.map(\.refresh), [false, false])
    }

    func testFailedVerificationDoesNotRetryAutomatically() async {
        let connection = MinutesModelFixtures.connection(providerID: .claudeAgentSDK)
        let candidate = MinutesModelFixtures.model(
            id: "claude-candidate", availability: .unverified, provider: "claude-agent-sdk")
        let client = FakeMinutesCatalogClient(
            connections: [connection], pages: ["first": MinutesModelFixtures.catalog(models: [candidate])],
            verificationFails: true)
        let store = MinutesModelCatalogStore(
            client: client, supportedProviders: ["claude-agent-sdk"], modelVerificationSupported: true)
        await store.loadCachedCatalog(connectionID: connection.connectionID)

        await store.verifyModel(
            connectionID: connection.connectionID, expectedRevision: connection.revision, expectedModel: candidate,
            confirmation: .sendTestPromptAndMayCharge)

        let verificationRequests = await client.verificationRequests
        XCTAssertEqual(verificationRequests.count, 1)
        XCTAssertNotNil(store.errorMessage)
        XCTAssertEqual(store.catalogs[connection.connectionID]?.models.first, candidate)
        XCTAssertTrue(store.verificationNotice?.contains("同じ要求 ID") == true)
    }

    func testUnknownVerificationReusesTheExactRequestAfterReconnect() async {
        let connection = MinutesModelFixtures.connection(providerID: .openAICompatibleAPI)
        let candidate = MinutesModelFixtures.model(
            id: "compatible-candidate", availability: .unverified, provider: "openai-compatible-api")
        let client = FakeMinutesCatalogClient(
            connections: [connection], pages: ["first": MinutesModelFixtures.catalog(models: [candidate])],
            verificationFailures: [.transport])
        let store = MinutesModelCatalogStore(
            client: client, supportedProviders: ["openai-compatible-api"], modelVerificationSupported: true)
        await store.loadCachedCatalog(connectionID: connection.connectionID)

        await store.verifyModel(
            connectionID: connection.connectionID, expectedRevision: connection.revision,
            expectedModel: candidate, confirmation: .sendTestPromptAndMayCharge)
        let original = await client.verificationRequests.first
        XCTAssertNotNil(original)

        store.invalidate()
        XCTAssertTrue(store.verificationNotice?.contains(original?.requestID ?? "missing") == true)
        store.setSupportedProviders(["openai-compatible-api"], modelVerificationSupported: true)
        await store.loadCachedCatalog(connectionID: connection.connectionID)
        await store.verifyModel(
            connectionID: connection.connectionID, expectedRevision: connection.revision,
            expectedModel: candidate, confirmation: .sendTestPromptAndMayCharge)

        let requests = await client.verificationRequests
        XCTAssertEqual(requests.count, 2)
        XCTAssertEqual(requests[0], requests[1])
        XCTAssertEqual(requests[0].requestID, original?.requestID)
        XCTAssertEqual(store.catalogs[connection.connectionID]?.models.first?.availability, .available)
    }

    func testUnknownVerificationBlocksChangedSnapshotInsteadOfCreatingAnotherRequest() async {
        let connection = MinutesModelFixtures.connection(providerID: .openAICompatibleAPI)
        let candidate = MinutesModelFixtures.model(
            id: "compatible-candidate", availability: .unverified, provider: "openai-compatible-api")
        let changed = MinutesModelFixtures.model(
            id: candidate.modelID, availability: .unverified, provider: "openai-compatible-api",
            maxOutputTokens: 2048)
        let client = FakeMinutesCatalogClient(
            connections: [connection], pages: ["first": MinutesModelFixtures.catalog(models: [candidate])],
            verificationFailures: [.transport])
        let store = MinutesModelCatalogStore(
            client: client, supportedProviders: ["openai-compatible-api"], modelVerificationSupported: true)
        await store.loadCachedCatalog(connectionID: connection.connectionID)
        await store.verifyModel(
            connectionID: connection.connectionID, expectedRevision: connection.revision,
            expectedModel: candidate, confirmation: .sendTestPromptAndMayCharge)

        await client.replace(
            connections: [connection], pages: ["first": MinutesModelFixtures.catalog(models: [changed])])
        await store.loadCachedCatalog(connectionID: connection.connectionID)
        XCTAssertFalse(store.isVerificationCandidate(
            connectionID: connection.connectionID, expectedRevision: connection.revision, model: changed))
        await store.verifyModel(
            connectionID: connection.connectionID, expectedRevision: connection.revision,
            expectedModel: changed, confirmation: .sendTestPromptAndMayCharge)

        let requests = await client.verificationRequests
        XCTAssertEqual(requests.count, 1)
        XCTAssertTrue(store.verificationNotice?.contains("未確定") == true)
    }

    func testCachedVerifiedModelReconcilesUnknownRequestBeforeAnotherProbe() async {
        let connection = MinutesModelFixtures.connection(providerID: .openAICompatibleAPI)
        let first = MinutesModelFixtures.model(
            id: "first-candidate", availability: .unverified, provider: "openai-compatible-api")
        let second = MinutesModelFixtures.model(
            id: "second-candidate", availability: .unverified, provider: "openai-compatible-api")
        let client = FakeMinutesCatalogClient(
            connections: [connection],
            pages: ["first": MinutesModelFixtures.catalog(models: [first, second])],
            verificationFailures: [.transport])
        let store = MinutesModelCatalogStore(
            client: client, supportedProviders: ["openai-compatible-api"], modelVerificationSupported: true)
        await store.loadCachedCatalog(connectionID: connection.connectionID)
        await store.verifyModel(
            connectionID: connection.connectionID, expectedRevision: connection.revision,
            expectedModel: first, confirmation: .sendTestPromptAndMayCharge)

        let verifiedFirst = MinutesModelFixtures.model(
            id: first.modelID, availability: .available, provider: "openai-compatible-api")
        await client.replace(
            connections: [connection],
            pages: ["first": MinutesModelFixtures.catalog(models: [verifiedFirst, second])])
        await store.loadCachedCatalog(connectionID: connection.connectionID)
        XCTAssertTrue(store.verificationNotice?.contains("完了") == true)
        XCTAssertTrue(store.isVerificationCandidate(
            connectionID: connection.connectionID, expectedRevision: connection.revision, model: second))

        await store.verifyModel(
            connectionID: connection.connectionID, expectedRevision: connection.revision,
            expectedModel: second, confirmation: .sendTestPromptAndMayCharge)
        let requests = await client.verificationRequests
        XCTAssertEqual(requests.count, 2)
        XCTAssertNotEqual(requests[0].requestID, requests[1].requestID)
        XCTAssertFalse(store.verificationNotice?.contains("未確定") == true)
    }

    func testPostProbeConfigurationConflictKeepsTheSameRequestID() async {
        let connection = MinutesModelFixtures.connection(providerID: .openAICompatibleAPI)
        let candidate = MinutesModelFixtures.model(
            id: "compatible-candidate", availability: .unverified, provider: "openai-compatible-api")
        let client = FakeMinutesCatalogClient(
            connections: [connection], pages: ["first": MinutesModelFixtures.catalog(models: [candidate])],
            verificationFailures: [.configurationConflict, .transport])
        let store = MinutesModelCatalogStore(
            client: client, supportedProviders: ["openai-compatible-api"], modelVerificationSupported: true)
        await store.loadCachedCatalog(connectionID: connection.connectionID)

        await store.verifyModel(
            connectionID: connection.connectionID, expectedRevision: connection.revision,
            expectedModel: candidate, confirmation: .sendTestPromptAndMayCharge)
        await store.verifyModel(
            connectionID: connection.connectionID, expectedRevision: connection.revision,
            expectedModel: candidate, confirmation: .sendTestPromptAndMayCharge)

        let requests = await client.verificationRequests
        XCTAssertEqual(requests.count, 2)
        XCTAssertEqual(requests[0].requestID, requests[1].requestID)
    }

    func testPreAcceptanceVerificationRejectionAllowsANewRequestID() async {
        let connection = MinutesModelFixtures.connection(providerID: .openAICompatibleAPI)
        let candidate = MinutesModelFixtures.model(
            id: "compatible-candidate", availability: .unverified, provider: "openai-compatible-api")
        let client = FakeMinutesCatalogClient(
            connections: [connection], pages: ["first": MinutesModelFixtures.catalog(models: [candidate])],
            verificationFailures: [.invalidArgument])
        let store = MinutesModelCatalogStore(
            client: client, supportedProviders: ["openai-compatible-api"], modelVerificationSupported: true)
        await store.loadCachedCatalog(connectionID: connection.connectionID)

        await store.verifyModel(
            connectionID: connection.connectionID, expectedRevision: connection.revision,
            expectedModel: candidate, confirmation: .sendTestPromptAndMayCharge)
        await store.verifyModel(
            connectionID: connection.connectionID, expectedRevision: connection.revision,
            expectedModel: candidate, confirmation: .sendTestPromptAndMayCharge)

        let requests = await client.verificationRequests
        XCTAssertEqual(requests.count, 2)
        XCTAssertNotEqual(requests[0].requestID, requests[1].requestID)
    }

    func testSuccessfulVerificationSurvivesCachedCatalogReloadFailureWithoutASecondCharge() async {
        let connection = MinutesModelFixtures.connection(providerID: .openAICompatibleAPI)
        let candidate = MinutesModelFixtures.model(
            id: "compatible-candidate", availability: .unverified, provider: "openai-compatible-api")
        let client = FakeMinutesCatalogClient(
            connections: [connection], pages: ["first": MinutesModelFixtures.catalog(models: [candidate])])
        let store = MinutesModelCatalogStore(
            client: client, supportedProviders: ["openai-compatible-api"], modelVerificationSupported: true)
        await store.loadCachedCatalog(connectionID: connection.connectionID)
        await client.failNextRequest()

        await store.verifyModel(
            connectionID: connection.connectionID, expectedRevision: connection.revision, expectedModel: candidate,
            confirmation: .sendTestPromptAndMayCharge)

        XCTAssertNil(store.errorMessage)
        XCTAssertTrue(store.verificationNotice?.contains("検証は完了") == true)
        XCTAssertEqual(store.catalogs[connection.connectionID]?.models.first {
            $0.modelID == candidate.modelID
        }?.availability, .available)
        await store.verifyModel(
            connectionID: connection.connectionID, expectedRevision: connection.revision, expectedModel: candidate,
            confirmation: .sendTestPromptAndMayCharge)
        let requests = await client.verificationRequests
        XCTAssertEqual(requests.count, 1)
    }

    func testVerificationNeverUsesAChangedConnectionRevision() async {
        let connection = MinutesModelFixtures.connection(providerID: .claudeAgentSDK)
        let candidate = MinutesModelFixtures.model(
            id: "claude-candidate", availability: .unverified, provider: "claude-agent-sdk")
        let client = FakeMinutesCatalogClient(
            connections: [connection], pages: ["first": MinutesModelFixtures.catalog(models: [candidate])])
        let store = MinutesModelCatalogStore(
            client: client, supportedProviders: ["claude-agent-sdk"], modelVerificationSupported: true)
        await store.loadCachedCatalog(connectionID: connection.connectionID)
        await store.verifyModel(
            connectionID: connection.connectionID, expectedRevision: connection.revision + 1, expectedModel: candidate,
            confirmation: .sendTestPromptAndMayCharge)
        let requests = await client.verificationRequests
        XCTAssertTrue(requests.isEmpty)
    }

    func testVerificationConfirmationRejectsAChangedModelDescriptor() async {
        let connection = MinutesModelFixtures.connection(providerID: .openAICompatibleAPI)
        let confirmed = MinutesModelFixtures.model(
            id: "compatible-candidate", availability: .unverified,
            provider: "openai-compatible-api", maxOutputTokens: nil)
        let changed = MinutesModelFixtures.model(
            id: confirmed.modelID, availability: .unverified,
            provider: "openai-compatible-api", maxOutputTokens: 2048)
        let client = FakeMinutesCatalogClient(
            connections: [connection], pages: ["first": MinutesModelFixtures.catalog(models: [confirmed])])
        let store = MinutesModelCatalogStore(
            client: client, supportedProviders: ["openai-compatible-api"], modelVerificationSupported: true)
        await store.loadCachedCatalog(connectionID: connection.connectionID)
        await client.replace(
            connections: [connection], pages: ["first": MinutesModelFixtures.catalog(models: [changed])])
        await store.loadCachedCatalog(connectionID: connection.connectionID)

        await store.verifyModel(
            connectionID: connection.connectionID, expectedRevision: connection.revision,
            expectedModel: confirmed, confirmation: .sendTestPromptAndMayCharge)

        let requests = await client.verificationRequests
        XCTAssertTrue(requests.isEmpty)
        XCTAssertTrue(store.verificationNotice?.contains("送信しませんでした") == true)
    }

    func testVerificationMergePreservesExistingPageOrder() async {
        let connection = MinutesModelFixtures.connection(providerID: .openAICompatibleAPI)
        let before = MinutesModelFixtures.model(id: "before", provider: "openai-compatible-api")
        let candidate = MinutesModelFixtures.model(
            id: "compatible-candidate", availability: .unverified, provider: "openai-compatible-api")
        let after = MinutesModelFixtures.model(id: "after", provider: "openai-compatible-api")
        let page = MinutesModelFixtures.catalog(models: [before, candidate, after], cursor: "next-page")
        let client = FakeMinutesCatalogClient(
            connections: [connection], pages: ["first": page], postVerificationPage: page)
        let store = MinutesModelCatalogStore(
            client: client, supportedProviders: ["openai-compatible-api"], modelVerificationSupported: true)
        await store.loadCachedCatalog(connectionID: connection.connectionID)

        await store.verifyModel(
            connectionID: connection.connectionID, expectedRevision: connection.revision,
            expectedModel: candidate, confirmation: .sendTestPromptAndMayCharge)

        XCTAssertEqual(
            store.catalogs[connection.connectionID]?.models.map(\.modelID),
            [before.modelID, candidate.modelID, after.modelID])
        XCTAssertEqual(store.catalogs[connection.connectionID]?.models[1].availability, .available)
        XCTAssertEqual(store.catalogs[connection.connectionID]?.nextCursor, "next-page")
    }

    func testLaterRetiredCatalogNeverGetsRevivedByVerificationReceipt() async {
        let connection = MinutesModelFixtures.connection(providerID: .claudeAgentSDK)
        let candidate = MinutesModelFixtures.model(
            id: "claude-candidate", availability: .unverified, provider: "claude-agent-sdk")
        let retired = MinutesModelFixtures.model(
            id: candidate.modelID, availability: .retired, provider: "claude-agent-sdk")
        let client = FakeMinutesCatalogClient(
            connections: [connection], pages: ["first": MinutesModelFixtures.catalog(models: [candidate])],
            postVerificationPage: MinutesModelFixtures.catalog(models: [retired]))
        let store = MinutesModelCatalogStore(
            client: client, supportedProviders: ["claude-agent-sdk"], modelVerificationSupported: true)
        await store.loadCachedCatalog(connectionID: connection.connectionID)

        await store.verifyModel(
            connectionID: connection.connectionID, expectedRevision: connection.revision,
            expectedModel: candidate, confirmation: .sendTestPromptAndMayCharge)

        XCTAssertEqual(store.catalogs[connection.connectionID]?.models.first?.availability, .retired)
        XCTAssertTrue(store.verificationNotice?.contains("利用不可") == true)
        let requests = await client.verificationRequests
        XCTAssertEqual(requests.count, 1)
    }

    func testLaterStaleCatalogIsNotPromotedOrReprobed() async {
        let connection = MinutesModelFixtures.connection(providerID: .openAICompatibleAPI)
        let candidate = MinutesModelFixtures.model(
            id: "compatible-candidate", availability: .unverified, provider: "openai-compatible-api")
        let client = FakeMinutesCatalogClient(
            connections: [connection], pages: ["first": MinutesModelFixtures.catalog(models: [candidate])],
            postVerificationPage: MinutesModelFixtures.catalog(models: [candidate], state: .stale))
        let store = MinutesModelCatalogStore(
            client: client, supportedProviders: ["openai-compatible-api"], modelVerificationSupported: true)
        await store.loadCachedCatalog(connectionID: connection.connectionID)

        await store.verifyModel(
            connectionID: connection.connectionID, expectedRevision: connection.revision,
            expectedModel: candidate, confirmation: .sendTestPromptAndMayCharge)
        await store.verifyModel(
            connectionID: connection.connectionID, expectedRevision: connection.revision,
            expectedModel: candidate, confirmation: .sendTestPromptAndMayCharge)

        XCTAssertEqual(store.catalogs[connection.connectionID]?.catalogState, .stale)
        XCTAssertEqual(store.catalogs[connection.connectionID]?.models.first?.availability, .unverified)
        let requests = await client.verificationRequests
        XCTAssertEqual(requests.count, 1)
    }

    func testLaterReadyDemotionCanBeExplicitlyReverified() async {
        let connection = MinutesModelFixtures.connection(providerID: .openAICompatibleAPI)
        let candidate = MinutesModelFixtures.model(
            id: "compatible-candidate", availability: .unverified, provider: "openai-compatible-api")
        let client = FakeMinutesCatalogClient(
            connections: [connection], pages: ["first": MinutesModelFixtures.catalog(models: [candidate])])
        let store = MinutesModelCatalogStore(
            client: client, supportedProviders: ["openai-compatible-api"], modelVerificationSupported: true)
        await store.loadCachedCatalog(connectionID: connection.connectionID)
        await store.verifyModel(
            connectionID: connection.connectionID, expectedRevision: connection.revision,
            expectedModel: candidate, confirmation: .sendTestPromptAndMayCharge)

        // A later server/runtime decision may revoke the previous proof while keeping
        // the refreshed catalog ready. The UI must allow a new explicit confirmation.
        await store.loadCachedCatalog(connectionID: connection.connectionID)
        XCTAssertTrue(store.isVerificationCandidate(
            connectionID: connection.connectionID, expectedRevision: connection.revision,
            model: candidate))
        await store.verifyModel(
            connectionID: connection.connectionID, expectedRevision: connection.revision,
            expectedModel: candidate, confirmation: .sendTestPromptAndMayCharge)

        let requests = await client.verificationRequests
        XCTAssertEqual(requests.count, 2)
    }

    func testCapabilityRevocationDiscardsLateCatalogAndBlocksSaving() async {
        let client = FakeMinutesCatalogClient(holdModels: true)
        let store = MinutesModelCatalogStore(client: client, supportedProviders: ["codex-app-server"])
        let load = Task { await store.loadCachedCatalog(connectionID: MinutesModelFixtures.connectionID) }
        await client.waitForModelRequest()
        store.setSupportedProviders([])
        await client.releaseModelRequest()
        await load.value
        XCTAssertTrue(store.catalogs.isEmpty)
        XCTAssertFalse(store.isLoading)
        XCTAssertNotNil(store.validationMessage(for: ProcessingConfigurationForm(config: MinutesModelFixtures.config())))
    }
}

private actor FakeMinutesCatalogClient: MinutesModelCatalogClient {
    private(set) var requests: [ListProviderModelsRequest] = []
    private(set) var verificationRequests: [VerifyProviderModelRequest] = []
    private var connections: [ProviderConnection]
    private var pages: [String: ListProviderModelsResponse]
    private let holdModels: Bool
    private let verificationFails: Bool
    private var verificationFailures: [ProviderSettingsErrorCode]
    private let postVerificationPage: ListProviderModelsResponse?
    private var shouldFail = false
    private var started: CheckedContinuation<Void, Never>?
    private var release: CheckedContinuation<Void, Never>?

    init(
        connections: [ProviderConnection] = [MinutesModelFixtures.connection()],
        pages: [String: ListProviderModelsResponse] = ["first": MinutesModelFixtures.catalog()],
        holdModels: Bool = false, verificationFails: Bool = false,
        verificationFailures: [ProviderSettingsErrorCode] = [],
        postVerificationPage: ListProviderModelsResponse? = nil
    ) {
        self.connections = connections
        self.pages = pages
        self.holdModels = holdModels
        self.verificationFails = verificationFails
        self.verificationFailures = verificationFailures
        self.postVerificationPage = postVerificationPage
    }

    func listProviderConnections() async throws -> ListProviderConnectionsResponse {
        ListProviderConnectionsResponse(connections: connections)
    }

    func listProviders() async throws -> ListProvidersResponse {
        ListProvidersResponse(providers: connections.map { ProviderSettingsFixtures.provider(providerID: $0.providerID) })
    }

    func listProviderModels(_ request: ListProviderModelsRequest) async throws -> ListProviderModelsResponse {
        requests.append(request)
        started?.resume()
        started = nil
        if holdModels { await withCheckedContinuation { release = $0 } }
        if shouldFail { throw FixtureUpstreamFailure() }
        guard let response = pages[request.cursor ?? "first"] else { throw ProviderSettingsFailure(.notFound) }
        return response
    }

    func verifyProviderModel(_ request: VerifyProviderModelRequest) async throws -> VerifyProviderModelResponse {
        verificationRequests.append(request)
        if !verificationFailures.isEmpty {
            throw ProviderSettingsFailure(verificationFailures.removeFirst())
        }
        if verificationFails { throw ProviderSettingsFailure(.transport) }
        guard let connection = connections.first(where: { $0.connectionID == request.connectionID }) else {
            throw ProviderSettingsFailure(.notFound)
        }
        let response = VerifyProviderModelResponse(
            connectionID: request.connectionID, connectionRevision: request.expectedRevision,
            model: MinutesModelFixtures.model(id: request.modelID, provider: connection.providerID.rawValue),
            catalogState: .ready, verifiedAt: ProviderSettingsFixtures.timestamp)
        if let postVerificationPage { pages["first"] = postVerificationPage }
        return response
    }

    func replace(connections: [ProviderConnection], pages: [String: ListProviderModelsResponse]) {
        self.connections = connections
        self.pages = pages
    }

    func waitForModelRequest() async {
        guard requests.isEmpty else { return }
        await withCheckedContinuation { started = $0 }
    }

    func releaseModelRequest() { release?.resume(); release = nil }
    func failNextRequest() { shouldFail = true }

    private struct FixtureUpstreamFailure: LocalizedError {
        var errorDescription: String? { "fixture-sensitive-upstream-text" }
    }
}
