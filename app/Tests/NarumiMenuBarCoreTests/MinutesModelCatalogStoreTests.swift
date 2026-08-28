import XCTest
@testable import NarumiMenuBarCore

@MainActor
final class MinutesModelCatalogStoreTests: XCTestCase {
    func testOpeningEditorOnlyReadsCachedMetadata() async {
        let client = FakeMinutesCatalogClient()
        let store = MinutesModelCatalogStore(client: client)
        await store.loadCachedCatalog(connectionID: MinutesModelFixtures.connectionID)
        let requests = await client.requests
        XCTAssertEqual(requests.count, 1)
        XCTAssertFalse(requests[0].refresh)
        XCTAssertEqual(requests[0].role, .llm)
        XCTAssertEqual(store.codexConnections.count, 1)
        XCTAssertNil(store.validationMessage(for: ProcessingConfigurationForm(config: MinutesModelFixtures.config())))
        XCTAssertFalse(store.isLoading)
    }

    func testOpeningEmptyDraftDoesNotFetchAnyModelsOrSelectAConnection() async {
        let client = FakeMinutesCatalogClient()
        let store = MinutesModelCatalogStore(client: client)
        await store.loadCachedCatalog(connectionID: "")
        let requests = await client.requests
        XCTAssertTrue(requests.isEmpty)
        XCTAssertEqual(store.codexConnections.count, 1)
        XCTAssertTrue(store.catalogs.isEmpty)
    }

    func testOnlyExplicitRefreshRequestsAnUpstreamCatalog() async {
        let client = FakeMinutesCatalogClient()
        let store = MinutesModelCatalogStore(client: client)
        await store.loadCachedCatalog(connectionID: MinutesModelFixtures.connectionID)
        await store.refreshModels(connectionID: MinutesModelFixtures.connectionID)
        let requests = await client.requests
        XCTAssertEqual(requests.map(\.refresh), [false, true])
        XCTAssertNil(store.errorMessage)
    }

    func testRevisionConflictDiscardsModelsInsteadOfAdoptingNewRevision() async {
        let client = FakeMinutesCatalogClient(pages: ["first": MinutesModelFixtures.catalog(revision: 2)])
        let store = MinutesModelCatalogStore(client: client)
        await store.loadCachedCatalog(connectionID: MinutesModelFixtures.connectionID)
        XCTAssertNil(store.catalogs[MinutesModelFixtures.connectionID])
        XCTAssertTrue(store.errorMessage?.contains("configuration_conflict") == true)
        XCTAssertNotNil(store.validationMessage(for: ProcessingConfigurationForm(config: MinutesModelFixtures.config())))
    }

    func testConnectionChangeInvalidatesOldCatalogAndLeavesSavedSelectionPinned() async {
        let client = FakeMinutesCatalogClient()
        let store = MinutesModelCatalogStore(client: client)
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
        let store = MinutesModelCatalogStore(client: client)
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
        let store = MinutesModelCatalogStore(client: client)
        await store.loadCachedCatalog(connectionID: MinutesModelFixtures.connectionID)
        await store.loadMoreModels(connectionID: MinutesModelFixtures.connectionID)
        XCTAssertEqual(store.catalogs[MinutesModelFixtures.connectionID]?.models.map(\.modelID), [MinutesModelFixtures.modelID, "second-model"])
        let requests = await client.requests
        XCTAssertTrue(requests.allSatisfy { !$0.refresh })
    }

    func testInvalidationDiscardsLateResultsAndBusyState() async {
        let client = FakeMinutesCatalogClient(holdModels: true)
        let store = MinutesModelCatalogStore(client: client)
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
        let store = MinutesModelCatalogStore(client: client)
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
        let store = MinutesModelCatalogStore(client: client)
        await store.loadCachedCatalog(connectionID: MinutesModelFixtures.connectionID)
        await client.failNextRequest()
        await store.refreshModels(connectionID: MinutesModelFixtures.connectionID)
        XCTAssertTrue(store.catalogs.isEmpty)
        XCTAssertFalse(store.errorMessage?.contains("fixture-sensitive-upstream-text") == true)
        XCTAssertTrue(store.errorMessage?.contains("internal") == true)
    }
}

private actor FakeMinutesCatalogClient: MinutesModelCatalogClient {
    private(set) var requests: [ListProviderModelsRequest] = []
    private var connections: [ProviderConnection]
    private var pages: [String: ListProviderModelsResponse]
    private let holdModels: Bool
    private var shouldFail = false
    private var started: CheckedContinuation<Void, Never>?
    private var release: CheckedContinuation<Void, Never>?

    init(
        connections: [ProviderConnection] = [MinutesModelFixtures.connection()],
        pages: [String: ListProviderModelsResponse] = ["first": MinutesModelFixtures.catalog()], holdModels: Bool = false
    ) {
        self.connections = connections
        self.pages = pages
        self.holdModels = holdModels
    }

    func listProviderConnections() async throws -> ListProviderConnectionsResponse {
        ListProviderConnectionsResponse(connections: connections)
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
