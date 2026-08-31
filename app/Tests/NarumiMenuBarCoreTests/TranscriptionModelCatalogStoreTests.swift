import XCTest
@testable import NarumiMenuBarCore

@MainActor
final class TranscriptionModelCatalogStoreTests: XCTestCase {
    func testOpeningEditorOnlyReadsCachedTranscriptionMetadata() async {
        let client = FakeTranscriptionCatalogClient()
        let store = makeStore(client)
        await store.loadCachedCatalog(connectionID: ASRCatalogFixture.connectionID)
        let requests = await client.requests
        XCTAssertEqual(requests.count, 1)
        XCTAssertFalse(requests[0].refresh)
        XCTAssertEqual(requests[0].role, .transcription)
        XCTAssertEqual(store.connections(for: "openai-api").count, 1)
        XCTAssertNil(store.validationMessage(for: ASRCatalogFixture.form()))
        XCTAssertFalse(store.isLoading)
    }

    func testEmptyDraftReadsMetadataWithoutChoosingConnectionOrFetchingModels() async {
        let client = FakeTranscriptionCatalogClient()
        let store = makeStore(client)
        await store.loadCachedCatalog(connectionID: "")
        let requests = await client.requests
        XCTAssertTrue(requests.isEmpty)
        XCTAssertEqual(store.connections(for: "openai-api").count, 1)
        XCTAssertTrue(store.catalogs.isEmpty)
    }

    func testOnlyExplicitRefreshRequestsUpstreamTranscriptionCatalog() async {
        let client = FakeTranscriptionCatalogClient()
        let store = makeStore(client)
        await store.loadCachedCatalog(connectionID: ASRCatalogFixture.connectionID)
        await store.refreshModels(connectionID: ASRCatalogFixture.connectionID)
        let requests = await client.requests
        XCTAssertEqual(requests.map(\.refresh), [false, true])
        XCTAssertTrue(requests.allSatisfy { $0.role == .transcription })
        XCTAssertTrue(requests.allSatisfy { $0.connectionID == ASRCatalogFixture.connectionID })
        XCTAssertNil(store.errorMessage)
    }

    func testInvalidConnectionsRemainReadableButCannotRefreshUpstream() async {
        let cases: [(ProviderConnection, [ProviderDescriptor])] = [
            (ASRCatalogFixture.connection(enabled: false), [ASRCatalogFixture.provider()]),
            (ASRCatalogFixture.connection(authState: .failed), [ASRCatalogFixture.provider()]),
            (ASRCatalogFixture.connection(credential: false), [ASRCatalogFixture.provider()]),
            (ASRCatalogFixture.connection(), [ASRCatalogFixture.provider(state: .notPrepared)]),
            (ASRCatalogFixture.connection(), [ASRCatalogFixture.provider(roles: [.llm])]),
            (ASRCatalogFixture.connection(), []),
        ]
        for (connection, providers) in cases {
            let client = FakeTranscriptionCatalogClient(connections: [connection], providers: providers)
            let store = makeStore(client)
            await store.loadCachedCatalog(connectionID: connection.connectionID)
            XCTAssertNotNil(store.catalogs[connection.connectionID])
            XCTAssertNotNil(store.connectionUnavailableReason(connection))
            await store.refreshModels(connectionID: connection.connectionID)
            let requests = await client.requests
            XCTAssertEqual(requests.map(\.refresh), [false])
            XCTAssertFalse(store.isLoading)
        }
    }

    func testOnlyAdvertisedOpenAIASRCapabilityCanFetchModels() async {
        let client = FakeTranscriptionCatalogClient()
        let store = TranscriptionModelCatalogStore(client: client)
        await store.loadCachedCatalog(connectionID: ASRCatalogFixture.connectionID)
        await store.refreshModels(connectionID: ASRCatalogFixture.connectionID)
        let initialRequests = await client.requests
        XCTAssertTrue(initialRequests.isEmpty)
        XCTAssertTrue(store.connections(for: "openai-api").isEmpty)
        store.setSupportedProviders(["ollama", "codex-app-server", "openai-api", "openai-api", "anthropic-api"])
        XCTAssertEqual(store.supportedProviders, ["openai-api"])
        await store.loadCachedCatalog(connectionID: ASRCatalogFixture.connectionID)
        store.setSupportedProviders([])
        await store.refreshModels(connectionID: ASRCatalogFixture.connectionID)
        await store.loadMoreModels(connectionID: ASRCatalogFixture.connectionID)
        let finalRequests = await client.requests
        XCTAssertEqual(finalRequests.count, 1)
        XCTAssertTrue(store.catalogs.isEmpty)
    }

    func testCodexConnectionCannotSupplyASRCatalog() async {
        let connection = ProviderSettingsFixtures.connection(providerID: .codexAppServer, authState: .authenticated)
        let client = FakeTranscriptionCatalogClient(connections: [connection])
        let store = makeStore(client)
        await store.loadCachedCatalog(connectionID: connection.connectionID)
        await store.refreshModels(connectionID: connection.connectionID)
        let requests = await client.requests
        XCTAssertTrue(requests.isEmpty)
        XCTAssertNotNil(store.connectionUnavailableReason(connection))
        XCTAssertTrue(store.connections(for: "codex-app-server").isEmpty)
    }

    func testValidationUsesASRSelectionPolicyAndEffectiveLanguage() async {
        let client = FakeTranscriptionCatalogClient()
        let store = makeStore(client)
        await store.loadCachedCatalog(connectionID: ASRCatalogFixture.connectionID)
        var form = ASRCatalogFixture.form()
        form.language = ""
        form.externalSendPolicy = ""
        XCTAssertNil(store.validationMessage(for: form))
        form.language = "ja-JP"
        XCTAssertNotNil(store.validationMessage(for: form))
        form.language = "ja"
        form.externalSendPolicy = "subscription_ok"
        XCTAssertNotNil(store.validationMessage(for: form))
        XCTAssertNil(store.validationMessage(for: ProcessingConfigurationForm()))
    }

    func testMismatchedResponseIdentityOrRevisionDiscardsPreviousCatalog() async {
        let mismatches = [
            ASRCatalogFixture.catalog(connectionID: "conn-ffffffffffff"),
            ASRCatalogFixture.catalog(revision: 2),
        ]
        for response in mismatches {
            let client = FakeTranscriptionCatalogClient()
            let store = makeStore(client)
            await store.loadCachedCatalog(connectionID: ASRCatalogFixture.connectionID)
            await client.replace(pages: ["first": response])
            await store.refreshModels(connectionID: ASRCatalogFixture.connectionID)
            XCTAssertTrue(store.catalogs.isEmpty)
            XCTAssertTrue(store.errorMessage?.contains("configuration_conflict") == true)
            XCTAssertFalse(store.isLoading)
        }
    }

    func testReloadingChangedConnectionDoesNotRewriteSavedSelection() async {
        let client = FakeTranscriptionCatalogClient()
        let store = makeStore(client)
        let form = ASRCatalogFixture.form()
        await store.loadCachedCatalog(connectionID: ASRCatalogFixture.connectionID)
        await client.replace(
            connections: [ASRCatalogFixture.connection(revision: 2)],
            pages: ["first": ASRCatalogFixture.catalog(revision: 2)])
        await store.loadCachedCatalog(connectionID: ASRCatalogFixture.connectionID)
        XCTAssertEqual(form.transcriptionModel.connectionRevision, 1)
        XCTAssertEqual(store.catalogs[ASRCatalogFixture.connectionID]?.connectionRevision, 2)
        XCTAssertNotNil(store.validationMessage(for: form))
    }

    func testDeletedConnectionRemovesItsCachedCatalogWithoutAnotherModelRead() async {
        let client = FakeTranscriptionCatalogClient()
        let store = makeStore(client)
        await store.loadCachedCatalog(connectionID: ASRCatalogFixture.connectionID)
        await client.replace(connections: [])
        await store.loadCachedCatalog(connectionID: ASRCatalogFixture.connectionID)
        let requests = await client.requests
        XCTAssertEqual(requests.count, 1)
        XCTAssertNil(store.connection(ASRCatalogFixture.connectionID))
        XCTAssertTrue(store.catalogs.isEmpty)
    }

    func testRefreshDiscardsModelsWhenLatestMetadataNoLongerMatches() async {
        let cases: [([ProviderConnection], [ProviderDescriptor])] = [
            ([], [ASRCatalogFixture.provider()]),
            ([ASRCatalogFixture.connection(revision: 2)], [ASRCatalogFixture.provider()]),
            ([ASRCatalogFixture.connection(authState: .failed)], [ASRCatalogFixture.provider()]),
            ([ASRCatalogFixture.connection()], [ASRCatalogFixture.provider(roles: [.llm])]),
        ]
        for (connections, providers) in cases {
            let client = FakeTranscriptionCatalogClient()
            let store = makeStore(client)
            await store.loadCachedCatalog(connectionID: ASRCatalogFixture.connectionID)
            await client.replace(connections: connections, providers: providers)
            await store.refreshModels(connectionID: ASRCatalogFixture.connectionID)
            XCTAssertTrue(store.catalogs.isEmpty)
            XCTAssertTrue(store.errorMessage?.contains("configuration_conflict") == true)
        }
    }

    func testSavedModelOnLaterPageLoadsOnlyCachedASRPages() async {
        let client = FakeTranscriptionCatalogClient(pages: [
            "first": ASRCatalogFixture.catalog(models: [ASRCatalogFixture.model("first-model")], cursor: "next-page"),
            "next-page": ASRCatalogFixture.catalog(cursor: "unneeded-page"),
        ])
        let store = makeStore(client)
        await store.loadCachedCatalog(connectionID: ASRCatalogFixture.connectionID, selectedModelID: "whisper-1")
        let requests = await client.requests
        XCTAssertEqual(requests.map(\.cursor), [nil, "next-page"])
        XCTAssertTrue(requests.allSatisfy { !$0.refresh && $0.role == .transcription })
        XCTAssertNil(store.validationMessage(for: ASRCatalogFixture.form()))
    }

    func testPaginationDeduplicatesWithinAndAcrossPages() async {
        let client = FakeTranscriptionCatalogClient(pages: [
            "first": ASRCatalogFixture.catalog(models: [ASRCatalogFixture.model(), ASRCatalogFixture.model()], cursor: "next-page"),
            "next-page": ASRCatalogFixture.catalog(models: [
                ASRCatalogFixture.model(), ASRCatalogFixture.model("second-model"), ASRCatalogFixture.model("second-model"),
            ]),
        ])
        let store = makeStore(client)
        await store.loadCachedCatalog(connectionID: ASRCatalogFixture.connectionID)
        await store.loadMoreModels(connectionID: ASRCatalogFixture.connectionID)
        await store.loadMoreModels(connectionID: ASRCatalogFixture.connectionID)
        let requests = await client.requests
        XCTAssertEqual(requests.map(\.cursor), [nil, "next-page"])
        XCTAssertTrue(requests.allSatisfy { !$0.refresh && $0.role == .transcription })
        XCTAssertEqual(store.catalogs[ASRCatalogFixture.connectionID]?.models.map(\.modelID), ["whisper-1", "second-model"])
    }

    func testCachedPaginationStopsOnRepeatedCursor() async {
        let client = FakeTranscriptionCatalogClient(pages: [
            "first": ASRCatalogFixture.catalog(models: [], cursor: "next-page"),
            "next-page": ASRCatalogFixture.catalog(models: [], cursor: "next-page"),
        ])
        let store = makeStore(client)
        await store.loadCachedCatalog(connectionID: ASRCatalogFixture.connectionID, selectedModelID: "whisper-1")
        let requests = await client.requests
        XCTAssertEqual(requests.count, 2)
        XCTAssertNil(store.catalogs[ASRCatalogFixture.connectionID]?.nextCursor)
        XCTAssertFalse(store.isLoading)
    }

    func testMissingSavedModelCannotCreateUnboundedAutomaticPagination() async {
        var pages = ["first": ASRCatalogFixture.catalog(models: [], cursor: "page-0")]
        for index in 0..<40 {
            pages["page-\(index)"] = ASRCatalogFixture.catalog(models: [], cursor: "page-\(index + 1)")
        }
        let client = FakeTranscriptionCatalogClient(pages: pages)
        let store = makeStore(client)
        await store.loadCachedCatalog(connectionID: ASRCatalogFixture.connectionID, selectedModelID: "whisper-1")
        let requests = await client.requests
        XCTAssertEqual(requests.count, 33)
        XCTAssertTrue(requests.allSatisfy { !$0.refresh })
        XCTAssertFalse(store.isLoading)
    }

    func testInvalidationClearsAllStateAndDiscardsLateResponse() async {
        let client = FakeTranscriptionCatalogClient(holdModels: true)
        let store = makeStore(client)
        let load = Task { await store.loadCachedCatalog(connectionID: ASRCatalogFixture.connectionID) }
        await client.waitForModelRequest()
        store.invalidate()
        await client.releaseModelRequest()
        await load.value
        XCTAssertTrue(store.connections.isEmpty)
        XCTAssertTrue(store.providers.isEmpty)
        XCTAssertTrue(store.supportedProviders.isEmpty)
        XCTAssertTrue(store.catalogs.isEmpty)
        XCTAssertFalse(store.isLoading)
        XCTAssertNil(store.errorMessage)
    }

    func testCapabilityRevocationDiscardsLateResponseAndBlocksSelection() async {
        let client = FakeTranscriptionCatalogClient(holdModels: true)
        let store = makeStore(client)
        let load = Task { await store.loadCachedCatalog(connectionID: ASRCatalogFixture.connectionID) }
        await client.waitForModelRequest()
        store.setSupportedProviders([])
        await client.releaseModelRequest()
        await load.value
        XCTAssertTrue(store.catalogs.isEmpty)
        XCTAssertFalse(store.isLoading)
        XCTAssertNotNil(store.validationMessage(for: ASRCatalogFixture.form()))
    }

    func testNewerLoadWinsOverOlderLateResponse() async {
        let client = FakeTranscriptionCatalogClient(holdModels: true)
        let store = makeStore(client)
        let older = Task { await store.loadCachedCatalog(connectionID: ASRCatalogFixture.connectionID) }
        await client.waitForModelRequest()
        await client.replace(
            connections: [ASRCatalogFixture.connection(revision: 2)],
            pages: ["first": ASRCatalogFixture.catalog(revision: 2)])
        await store.loadCachedCatalog(connectionID: ASRCatalogFixture.connectionID)
        await client.releaseModelRequest()
        await older.value
        XCTAssertEqual(store.connection(ASRCatalogFixture.connectionID)?.revision, 2)
        XCTAssertEqual(store.catalogs[ASRCatalogFixture.connectionID]?.connectionRevision, 2)
        XCTAssertNil(store.errorMessage)
        XCTAssertFalse(store.isLoading)
    }

    func testCancelledReadDiscardsLateSuccessOrFailureAndReleasesBusyState() async {
        for fail in [false, true] {
            let client = FakeTranscriptionCatalogClient(holdModels: true)
            let store = makeStore(client)
            let load = Task { await store.loadCachedCatalog(connectionID: ASRCatalogFixture.connectionID) }
            await client.waitForModelRequest()
            if fail { await client.failModels() }
            load.cancel()
            await client.releaseModelRequest()
            await load.value
            XCTAssertFalse(store.isLoading)
            XCTAssertTrue(store.catalogs.isEmpty)
            XCTAssertNil(store.errorMessage)
        }
    }

    func testCancelledRefreshDoesNotStartFollowupMetadataReads() async {
        let client = FakeTranscriptionCatalogClient()
        let store = makeStore(client)
        await store.loadCachedCatalog(connectionID: ASRCatalogFixture.connectionID)
        await client.holdNextModelRequest()
        let refresh = Task { await store.refreshModels(connectionID: ASRCatalogFixture.connectionID) }
        await client.waitForModelRequest(count: 2)
        refresh.cancel()
        await client.releaseModelRequest()
        await refresh.value
        let connectionReads = await client.connectionReads
        let providerReads = await client.providerReads
        XCTAssertEqual(connectionReads, 1)
        XCTAssertEqual(providerReads, 1)
        XCTAssertFalse(store.isLoading)
        XCTAssertNil(store.errorMessage)
    }

    func testFailedMetadataRequestClearsCatalogWithoutRevealingUpstreamText() async {
        let client = FakeTranscriptionCatalogClient()
        let store = makeStore(client)
        await store.loadCachedCatalog(connectionID: ASRCatalogFixture.connectionID)
        await client.failModels()
        await store.refreshModels(connectionID: ASRCatalogFixture.connectionID)
        XCTAssertTrue(store.catalogs.isEmpty)
        XCTAssertEqual(store.errorMessage, ProviderSettingsFailure(.internalError).message)
        XCTAssertFalse(store.errorMessage?.contains("fixture-sensitive-upstream-text") == true)
        XCTAssertFalse(store.isLoading)
    }

    func testASRCatalogIsIndependentOfMinutesCatalogForSameConnection() async {
        let client = FakeTranscriptionCatalogClient()
        let store = makeStore(client)
        let minutes = MinutesModelCatalogStore(client: client, supportedProviders: ["openai-api"])
        await minutes.loadCachedCatalog(connectionID: ASRCatalogFixture.connectionID)
        XCTAssertTrue(store.catalogs.isEmpty)
        await store.loadCachedCatalog(connectionID: ASRCatalogFixture.connectionID)
        minutes.invalidate()
        XCTAssertNotNil(store.catalogs[ASRCatalogFixture.connectionID])
        let requests = await client.requests
        XCTAssertEqual(requests.map(\.role), [.llm, .transcription])
    }

    private func makeStore(_ client: FakeTranscriptionCatalogClient) -> TranscriptionModelCatalogStore {
        TranscriptionModelCatalogStore(client: client, supportedProviders: ["openai-api"])
    }
}

private enum ASRCatalogFixture {
    static let connectionID = ProviderSettingsFixtures.connectionID

    static func connection(
        revision: Int = 1, enabled: Bool = true, authState: ProviderAuthState = .authenticated,
        credential: Bool = true
    ) -> ProviderConnection {
        ProviderSettingsFixtures.connection(
            revision: revision, providerID: .openaiAPI, enabled: enabled, credential: credential,
            authState: authState, catalogState: .ready)
    }

    static func provider(roles: [ProviderRole] = [.transcription], state: ProviderRuntimeState = .ready) -> ProviderDescriptor {
        let base = ProviderSettingsFixtures.provider(providerID: .openaiAPI, state: state)
        return ProviderDescriptor(
            providerID: base.providerID, displayName: base.displayName, roles: roles,
            authMethods: base.authMethods, availability: base.availability, reason: base.reason, runtime: base.runtime)
    }

    static func model(_ id: String = "whisper-1") -> ProviderModelDescriptor {
        ProviderModelDescriptor(
            modelID: id, displayName: "Fixture transcription model", resolvedRevision: nil,
            inputModalities: [.audio], outputModalities: [.text], roles: [.transcription],
            timestampSupport: .word, contextWindow: nil, maxOutputTokens: nil,
            parameterSchema: ProviderParameterSchema(), availability: .available, reason: nil,
            source: .providerAPI, fetchedAt: ProviderSettingsFixtures.timestamp,
            billing: ProviderModelBilling(
                kind: .api, inputUSDPerMillionTokens: nil, outputUSDPerMillionTokens: nil,
                audioUSDPerMinute: nil, fetchedAt: nil))
    }

    static func catalog(
        connectionID: String = ASRCatalogFixture.connectionID, revision: Int = 1,
        models: [ProviderModelDescriptor] = [model()], cursor: String? = nil
    ) -> ListProviderModelsResponse {
        ListProviderModelsResponse(
            connectionID: connectionID, connectionRevision: revision, models: models,
            nextCursor: cursor, catalogState: .ready, fetchedAt: ProviderSettingsFixtures.timestamp)
    }

    static func form() -> ProcessingConfigurationForm {
        ProcessingConfigurationForm(config: MeetingConfig(
            transcriptionEngine: "auto", externalSendPolicy: "api_ok", language: "ja",
            transcriptionModel: TranscriptionModelSelection(
                connectionID: connectionID, connectionRevision: 1, modelID: "whisper-1")))
    }
}

private actor FakeTranscriptionCatalogClient: TranscriptionModelCatalogClient, MinutesModelCatalogClient {
    private(set) var requests: [ListProviderModelsRequest] = []
    private(set) var connectionReads = 0
    private(set) var providerReads = 0
    private var connections: [ProviderConnection]
    private var providers: [ProviderDescriptor]
    private var pages: [String: ListProviderModelsResponse]
    private var holdModels: Bool
    private var shouldFailModels = false
    private var started: CheckedContinuation<Void, Never>?
    private var waitCount = 1
    private var release: CheckedContinuation<Void, Never>?

    init(
        connections: [ProviderConnection] = [ASRCatalogFixture.connection()],
        providers: [ProviderDescriptor] = [ASRCatalogFixture.provider()],
        pages: [String: ListProviderModelsResponse] = ["first": ASRCatalogFixture.catalog()], holdModels: Bool = false
    ) {
        self.connections = connections
        self.providers = providers
        self.pages = pages
        self.holdModels = holdModels
    }

    func listProviderConnections() async throws -> ListProviderConnectionsResponse {
        connectionReads += 1
        return ListProviderConnectionsResponse(connections: connections)
    }

    func listProviders() async throws -> ListProvidersResponse {
        providerReads += 1
        return ListProvidersResponse(providers: providers)
    }

    func listProviderModels(_ request: ListProviderModelsRequest) async throws -> ListProviderModelsResponse {
        requests.append(request)
        let response = pages[request.cursor ?? "first"]
        if requests.count >= waitCount { started?.resume(); started = nil }
        if holdModels {
            holdModels = false
            await withCheckedContinuation { release = $0 }
        }
        if shouldFailModels { throw FixtureUpstreamFailure() }
        guard let response else { throw ProviderSettingsFailure(.notFound) }
        return response
    }

    func replace(
        connections: [ProviderConnection]? = nil, providers: [ProviderDescriptor]? = nil,
        pages: [String: ListProviderModelsResponse]? = nil
    ) {
        if let connections { self.connections = connections }
        if let providers { self.providers = providers }
        if let pages { self.pages = pages }
    }

    func waitForModelRequest(count: Int = 1) async {
        guard requests.count < count else { return }
        waitCount = count
        await withCheckedContinuation { started = $0 }
    }

    func releaseModelRequest() { release?.resume(); release = nil }
    func holdNextModelRequest() { holdModels = true }
    func failModels() { shouldFailModels = true }

    private struct FixtureUpstreamFailure: LocalizedError {
        var errorDescription: String? { "fixture-sensitive-upstream-text" }
    }
}
