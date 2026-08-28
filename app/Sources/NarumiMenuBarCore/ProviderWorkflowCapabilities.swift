import Foundation

/// Implemented workflow surfaces. Discovery never implies that generation is configured.
public struct ProviderWorkflowCapabilities: Codable, Equatable, Sendable {
    public var providerConnections: Bool
    public var providerModels: Bool
    public var stageModelSelection: Bool
    public var ensembleGeneration: Bool

    enum CodingKeys: String, CodingKey {
        case providerConnections = "provider_connections"
        case providerModels = "provider_models"
        case stageModelSelection = "stage_model_selection"
        case ensembleGeneration = "ensemble_generation"
    }

    public init(
        providerConnections: Bool, providerModels: Bool,
        stageModelSelection: Bool, ensembleGeneration: Bool
    ) {
        self.providerConnections = providerConnections
        self.providerModels = providerModels
        self.stageModelSelection = stageModelSelection
        self.ensembleGeneration = ensembleGeneration
    }
}

/// Informational transport requirements; these fields do not establish server trust.
public struct SecureTransportMetadata: Codable, Equatable, Sendable {
    public var mode: String
    public var tlsRequired: Bool
    public var clientAuthRequired: Bool

    enum CodingKeys: String, CodingKey {
        case mode
        case tlsRequired = "tls_required"
        case clientAuthRequired = "client_auth_required"
    }

    public init(mode: String, tlsRequired: Bool, clientAuthRequired: Bool) {
        self.mode = mode
        self.tlsRequired = tlsRequired
        self.clientAuthRequired = clientAuthRequired
    }
}
