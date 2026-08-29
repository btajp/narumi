import Foundation
import Observation

/// This surface cannot authenticate, change a connection, or generate a meeting.
public protocol MinutesModelCatalogClient: Sendable {
    func listProviders() async throws -> ListProvidersResponse
    func listProviderConnections() async throws -> ListProviderConnectionsResponse
    func listProviderModels(_ request: ListProviderModelsRequest) async throws -> ListProviderModelsResponse
}

@MainActor
@Observable
public final class MinutesModelCatalogStore {
    public private(set) var connections: [ProviderConnection] = []
    public private(set) var providers: [ProviderDescriptor] = []
    public private(set) var supportedProviders: [String]
    public private(set) var catalogs: [String: ListProviderModelsResponse] = [:]
    public private(set) var isLoading = false
    public private(set) var errorMessage: String?
    @ObservationIgnored private let client: any MinutesModelCatalogClient
    @ObservationIgnored private var generation: UInt64 = 0

    public init(client: any MinutesModelCatalogClient, supportedProviders: [String] = []) {
        self.client = client
        self.supportedProviders = MinutesModelSelection.providers.filter { supportedProviders.contains($0) }
    }

    public func setSupportedProviders(_ values: [String]) {
        let next = MinutesModelSelection.providers.filter { values.contains($0) }
        guard next != supportedProviders else { return }
        generation &+= 1
        isLoading = false
        supportedProviders = next
        catalogs = catalogs.filter { id, _ in
            connections.contains { $0.connectionID == id && next.contains($0.providerID.rawValue) }
        }
    }

    public func connections(for provider: String) -> [ProviderConnection] {
        guard supportedProviders.contains(provider) else { return [] }
        return connections.filter { $0.providerID.rawValue == provider }
    }

    public func connectionUnavailableReason(_ connection: ProviderConnection) -> String? {
        guard supportedProviders.contains(connection.providerID.rawValue) else {
            return "このサーバーはこのプロバイダの議事録生成に対応していません。"
        }
        return MinutesModelForm.connectionUnavailableReason(connection, providers: providers)
    }

    public func connection(_ id: String) -> ProviderConnection? {
        connections.first { $0.connectionID == id }
    }

    public func validationMessage(for form: ProcessingConfigurationForm) -> String? {
        form.minutesModel.validationMessage(
            connections: connections, catalog: catalogs[form.minutesModel.connectionID],
            externalSendPolicy: form.effectiveExternalSendPolicy,
            supportedProviders: supportedProviders, providers: providers)
    }

    /// Opening a form only reads saved metadata. It does not refresh upstream model catalogs.
    public func loadCachedCatalog(connectionID: String, selectedModelID: String? = nil) async {
        generation &+= 1
        let token = generation
        isLoading = true
        errorMessage = nil
        defer { finish(token) }
        do {
            let response = try await client.listProviderConnections()
            guard isCurrent(token) else { return }
            let providerResponse = try await client.listProviders()
            guard isCurrent(token) else { return }
            connections = response.connections
            providers = providerResponse.providers
            catalogs = catalogs.filter { id, catalog in
                connections.contains { $0.connectionID == id && $0.revision == catalog.connectionRevision
                    && supportedProviders.contains($0.providerID.rawValue) }
            }
            if let selected = connection(connectionID), supportedProviders.contains(selected.providerID.rawValue) {
                try await fetchModels(connection: selected, refresh: false, cursor: nil, token: token)
                var cursors: Set<String> = []
                while let selectedModelID, !selectedModelID.isEmpty,
                    catalogs[connectionID]?.models.contains(where: { $0.modelID == selectedModelID }) != true,
                    let cursor = catalogs[connectionID]?.nextCursor,
                    cursors.insert(cursor).inserted, cursors.count <= 32 {
                    guard isCurrent(token) else { return }
                    try await fetchModels(connection: selected, refresh: false, cursor: cursor, token: token)
                }
            }
            finish(token)
        } catch { fail(error, connectionID: connectionID, token: token) }
    }

    /// Only the explicit "モデル候補を取得・更新" button calls this method with refresh=true.
    public func refreshModels(connectionID: String) async {
        guard !isLoading, let selected = connection(connectionID), selected.enabled,
            connectionUnavailableReason(selected) == nil else { return }
        generation &+= 1
        let token = generation
        isLoading = true
        errorMessage = nil
        defer { finish(token) }
        do {
            try await fetchModels(connection: selected, refresh: true, cursor: nil, token: token)
            // A successful explicit refresh can also update the saved authentication status.
            let response = try await client.listProviderConnections()
            guard isCurrent(token) else { return }
            let providerResponse = try await client.listProviders()
            guard isCurrent(token) else { return }
            connections = response.connections
            providers = providerResponse.providers
            if connection(connectionID)?.revision != selected.revision {
                throw ProviderSettingsFailure(.configurationConflict)
            }
            finish(token)
        } catch { fail(error, connectionID: connectionID, token: token) }
    }

    public func loadMoreModels(connectionID: String) async {
        guard !isLoading, let selected = connection(connectionID),
            supportedProviders.contains(selected.providerID.rawValue),
            let cursor = catalogs[connectionID]?.nextCursor else { return }
        generation &+= 1
        let token = generation
        isLoading = true
        errorMessage = nil
        defer { finish(token) }
        do {
            try await fetchModels(connection: selected, refresh: false, cursor: cursor, token: token)
            finish(token)
        } catch { fail(error, connectionID: connectionID, token: token) }
    }

    public func invalidate() {
        generation &+= 1
        connections = []
        providers = []
        supportedProviders = []
        catalogs = [:]
        isLoading = false
        errorMessage = nil
    }

    private func fetchModels(
        connection: ProviderConnection, refresh: Bool, cursor: String?, token: UInt64
    ) async throws {
        let response = try await client.listProviderModels(ListProviderModelsRequest(
            connectionID: connection.connectionID, role: .llm, cursor: cursor, refresh: refresh))
        guard isCurrent(token) else { return }
        guard response.connectionID == connection.connectionID, response.connectionRevision == connection.revision else {
            throw ProviderSettingsFailure(.configurationConflict)
        }
        var models = response.models
        if cursor != nil, let existing = catalogs[connection.connectionID] {
            let known = Set(existing.models.map(\.modelID))
            models = existing.models + models.filter { !known.contains($0.modelID) }
        }
        catalogs[connection.connectionID] = ListProviderModelsResponse(
            connectionID: response.connectionID, connectionRevision: response.connectionRevision,
            models: models, nextCursor: response.nextCursor == cursor ? nil : response.nextCursor,
            catalogState: response.catalogState, fetchedAt: response.fetchedAt)
    }

    private func isCurrent(_ token: UInt64) -> Bool { token == generation && !Task.isCancelled }

    private func finish(_ token: UInt64) {
        guard token == generation else { return }
        isLoading = false
    }

    private func fail(_ error: Error, connectionID: String, token: UInt64) {
        guard token == generation else { return }
        catalogs[connectionID] = nil
        errorMessage = (error as? ProviderSettingsFailure ?? ProviderSettingsFailure(.internalError)).message
        isLoading = false
    }
}
