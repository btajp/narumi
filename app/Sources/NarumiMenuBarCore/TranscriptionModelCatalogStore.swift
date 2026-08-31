import Foundation
import Observation

/// Reads ASR metadata only; this surface cannot authenticate or submit audio.
public protocol TranscriptionModelCatalogClient: Sendable {
    func listProviders() async throws -> ListProvidersResponse
    func listProviderConnections() async throws -> ListProviderConnectionsResponse
    func listProviderModels(_ request: ListProviderModelsRequest) async throws -> ListProviderModelsResponse
}

@MainActor
@Observable
public final class TranscriptionModelCatalogStore {
    public private(set) var connections: [ProviderConnection] = []
    public private(set) var providers: [ProviderDescriptor] = []
    public private(set) var supportedProviders: [String]
    public private(set) var catalogs: [String: ListProviderModelsResponse] = [:]
    public private(set) var isLoading = false
    public private(set) var errorMessage: String?
    @ObservationIgnored private let client: any TranscriptionModelCatalogClient
    @ObservationIgnored private var generation: UInt64 = 0

    public init(client: any TranscriptionModelCatalogClient, supportedProviders: [String] = []) {
        self.client = client
        self.supportedProviders = TranscriptionModelSelection.providers.filter { supportedProviders.contains($0) }
    }

    public func setSupportedProviders(_ values: [String]) {
        let next = TranscriptionModelSelection.providers.filter { values.contains($0) }
        guard next != supportedProviders else { return }
        generation &+= 1
        isLoading = false
        errorMessage = nil
        supportedProviders = next
        discardObsoleteCatalogs()
    }

    public func connections(for provider: String) -> [ProviderConnection] {
        guard supportedProviders.contains(provider) else { return [] }
        return connections.filter { $0.providerID.rawValue == provider }
    }

    public func connection(_ id: String) -> ProviderConnection? {
        connections.first { $0.connectionID == id }
    }

    public func connectionUnavailableReason(_ connection: ProviderConnection) -> String? {
        guard supportedProviders.contains(connection.providerID.rawValue) else {
            return "このサーバーはこのプロバイダの音声認識に対応していません。"
        }
        return TranscriptionModelForm.connectionUnavailableReason(connection, providers: providers)
    }

    public func validationMessage(for form: ProcessingConfigurationForm) -> String? {
        form.transcriptionModel.validationMessage(
            connections: connections, catalog: catalogs[form.transcriptionModel.connectionID],
            externalSendPolicy: form.effectiveExternalSendPolicy, language: form.effectiveLanguage,
            supportedProviders: supportedProviders, providers: providers)
    }

    /// A saved, disabled connection can still expose cached metadata without an upstream request.
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
            discardObsoleteCatalogs()
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
        } catch { fail(error, connectionID: connectionID, token: token) }
    }

    /// Only an explicit user action may refresh the provider's ASR model catalog.
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
            guard isCurrent(token) else { return }
            let response = try await client.listProviderConnections()
            guard isCurrent(token) else { return }
            let providerResponse = try await client.listProviders()
            guard isCurrent(token) else { return }
            connections = response.connections
            providers = providerResponse.providers
            discardObsoleteCatalogs()
            guard let current = connection(connectionID), current.revision == selected.revision,
                current.providerID == selected.providerID, connectionUnavailableReason(current) == nil else {
                throw ProviderSettingsFailure(.configurationConflict)
            }
        } catch { fail(error, connectionID: connectionID, token: token) }
    }

    public func loadMoreModels(connectionID: String) async {
        guard !isLoading, let selected = connection(connectionID),
            supportedProviders.contains(selected.providerID.rawValue),
            let catalog = catalogs[connectionID], catalog.connectionRevision == selected.revision,
            let cursor = catalog.nextCursor else { return }
        generation &+= 1
        let token = generation
        isLoading = true
        errorMessage = nil
        defer { finish(token) }
        do {
            try await fetchModels(connection: selected, refresh: false, cursor: cursor, token: token)
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
            connectionID: connection.connectionID, role: .transcription, cursor: cursor, refresh: refresh))
        guard isCurrent(token) else { return }
        guard response.connectionID == connection.connectionID, response.connectionRevision == connection.revision else {
            throw ProviderSettingsFailure(.configurationConflict)
        }
        let previous = cursor == nil ? [] : (catalogs[connection.connectionID]?.models ?? [])
        var known: Set<String> = []
        let models = (previous + response.models).filter { known.insert($0.modelID).inserted }
        catalogs[connection.connectionID] = ListProviderModelsResponse(
            connectionID: response.connectionID, connectionRevision: response.connectionRevision,
            models: models, nextCursor: response.nextCursor == cursor ? nil : response.nextCursor,
            catalogState: response.catalogState, fetchedAt: response.fetchedAt)
    }

    private func discardObsoleteCatalogs() {
        catalogs = catalogs.filter { id, catalog in
            catalog.connectionID == id && connections.contains {
                $0.connectionID == id && $0.revision == catalog.connectionRevision
                    && supportedProviders.contains($0.providerID.rawValue)
            }
        }
    }

    private func isCurrent(_ token: UInt64) -> Bool { token == generation && !Task.isCancelled }

    private func finish(_ token: UInt64) {
        guard token == generation else { return }
        isLoading = false
    }

    private func fail(_ error: Error, connectionID: String, token: UInt64) {
        guard isCurrent(token) else { return }
        catalogs[connectionID] = nil
        errorMessage = (error as? ProviderSettingsFailure ?? ProviderSettingsFailure(.internalError)).message
    }
}
