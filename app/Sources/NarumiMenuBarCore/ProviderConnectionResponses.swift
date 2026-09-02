import Foundation

public struct ListProvidersResponse: Decodable, Equatable, Sendable {
    public let providers: [ProviderDescriptor]

    public init(providers: [ProviderDescriptor]) {
        self.providers = providers
    }
}

public struct ListProviderConnectionsResponse: Decodable, Equatable, Sendable {
    public let connections: [ProviderConnection]

    public init(connections: [ProviderConnection]) {
        self.connections = connections
    }
}

public struct ProviderConnectionResponse: Decodable, Equatable, Sendable {
    public let connection: ProviderConnection

    public init(connection: ProviderConnection) {
        self.connection = connection
    }
}

public struct DeleteProviderConnectionResponse: Decodable, Equatable, Sendable {
    public let connectionID: String
    public let deleted: Bool

    enum CodingKeys: String, CodingKey {
        case connectionID = "connection_id"
        case deleted
    }

    public init(connectionID: String, deleted: Bool) {
        self.connectionID = connectionID
        self.deleted = deleted
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        connectionID = try container.decode(String.self, forKey: .connectionID)
        deleted = try container.decode(Bool.self, forKey: .deleted)
        guard deleted else {
            throw DecodingError.dataCorruptedError(
                forKey: .deleted, in: container,
                debugDescription: "A deletion receipt must confirm deletion")
        }
    }
}

public struct ProviderAuthResponse: Decodable, Equatable, Sendable {
    public let operation: ProviderAuthOperation

    public init(operation: ProviderAuthOperation) {
        self.operation = operation
    }
}

public struct ProviderConnectionTestResult: Decodable, Equatable, Sendable {
    public let connection: ProviderConnection
    public let connected: Bool
    public let reason: String?

    enum CodingKeys: String, CodingKey {
        case connection, connected, reason
    }

    public init(connection: ProviderConnection, connected: Bool, reason: String?) {
        self.connection = connection
        self.connected = connected
        self.reason = reason
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        connection = try container.decode(ProviderConnection.self, forKey: .connection)
        connected = try container.decode(Bool.self, forKey: .connected)
        reason = try container.decode(String?.self, forKey: .reason)
    }
}

public struct ListProviderModelsResponse: Decodable, Equatable, Sendable {
    public let connectionID: String
    public let connectionRevision: Int
    public let models: [ProviderModelDescriptor]
    public let nextCursor: String?
    public let catalogState: ProviderCatalogState
    public let fetchedAt: String?

    enum CodingKeys: String, CodingKey {
        case connectionID = "connection_id"
        case connectionRevision = "connection_revision"
        case models
        case nextCursor = "next_cursor"
        case catalogState = "catalog_state"
        case fetchedAt = "fetched_at"
    }

    public init(
        connectionID: String, connectionRevision: Int, models: [ProviderModelDescriptor],
        nextCursor: String?, catalogState: ProviderCatalogState, fetchedAt: String?
    ) {
        self.connectionID = connectionID
        self.connectionRevision = connectionRevision
        self.models = models
        self.nextCursor = nextCursor
        self.catalogState = catalogState
        self.fetchedAt = fetchedAt
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        connectionID = try container.decode(String.self, forKey: .connectionID)
        connectionRevision = try container.decode(Int.self, forKey: .connectionRevision)
        models = try container.decode([ProviderModelDescriptor].self, forKey: .models)
        nextCursor = try container.decode(String?.self, forKey: .nextCursor)
        catalogState = try container.decode(ProviderCatalogState.self, forKey: .catalogState)
        fetchedAt = try container.decode(String?.self, forKey: .fetchedAt)
        guard connectionRevision > 0 else {
            throw DecodingError.dataCorruptedError(
                forKey: .connectionRevision, in: container,
                debugDescription: "Connection revisions must be positive")
        }
    }
}

public struct VerifyProviderModelResponse: Decodable, Equatable, Sendable {
    public let connectionID: String
    public let connectionRevision: Int
    public let model: ProviderModelDescriptor
    public let catalogState: ProviderCatalogState
    public let verifiedAt: String

    enum CodingKeys: String, CodingKey {
        case connectionID = "connection_id"
        case connectionRevision = "connection_revision"
        case model
        case catalogState = "catalog_state"
        case verifiedAt = "verified_at"
    }

    public init(
        connectionID: String, connectionRevision: Int, model: ProviderModelDescriptor,
        catalogState: ProviderCatalogState, verifiedAt: String
    ) {
        self.connectionID = connectionID
        self.connectionRevision = connectionRevision
        self.model = model
        self.catalogState = catalogState
        self.verifiedAt = verifiedAt
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        connectionID = try container.decode(String.self, forKey: .connectionID)
        connectionRevision = try container.decode(Int.self, forKey: .connectionRevision)
        model = try container.decode(ProviderModelDescriptor.self, forKey: .model)
        catalogState = try container.decode(ProviderCatalogState.self, forKey: .catalogState)
        verifiedAt = try container.decode(String.self, forKey: .verifiedAt)
        guard connectionRevision > 0, model.availability == .available,
            catalogState == .ready, !verifiedAt.isEmpty else {
            throw DecodingError.dataCorruptedError(
                forKey: .model, in: container,
                debugDescription: "A successful model verification must return an available cached model")
        }
    }
}

public struct PrepareProviderRuntimeResponse: Decodable, Equatable, Sendable {
    public let jobID: String

    enum CodingKeys: String, CodingKey {
        case jobID = "job_id"
    }

    public init(jobID: String) {
        self.jobID = jobID
    }
}
