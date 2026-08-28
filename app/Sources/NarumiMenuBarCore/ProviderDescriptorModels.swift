import Foundation

public enum ProviderID: String, Codable, CaseIterable, Sendable {
    case anthropicAPI = "anthropic-api"
    case openaiAPI = "openai-api"
    case claudeAgentSDK = "claude-agent-sdk"
    case ollama
    case codexAppServer = "codex-app-server"

    public var supportedAuthMethod: ProviderAuthMethod {
        switch self {
        case .anthropicAPI, .openaiAPI, .claudeAgentSDK: return .apiKey
        case .ollama: return .none
        case .codexAppServer: return .chatgpt
        }
    }
}

public enum ProviderRole: String, Codable, CaseIterable, Sendable {
    case transcription, diarization, llm
}

public enum ProviderAuthMethod: String, Codable, CaseIterable, Sendable {
    case apiKey = "api_key"
    case none
    case chatgpt
}

public enum ProviderAvailability: String, Codable, CaseIterable, Sendable {
    case available
    case notPrepared = "not_prepared"
    case unverified
    case authenticationRequired = "authentication_required"
    case unsupported, retired
}

public enum ProviderRuntimeState: String, Codable, Sendable {
    case ready
    case notPrepared = "not_prepared"
    case preparing, unavailable, failed, unknown
}

public enum ProviderSetupState: String, Codable, Sendable {
    case queued, running, succeeded, failed, cancelled, unknown
}

public enum ProviderRuntimeResourceKind: String, Codable, Sendable {
    case runtime, model
}

public enum ProviderRuntimeResourceSource: String, Codable, Sendable {
    case bundled, installed
    case approvedDownload = "approved_download"
}

public struct ProviderSetupOperation: Decodable, Equatable, Sendable {
    public let jobID: String
    public let startRequestID: String
    public let resourceID: String
    public let state: ProviderSetupState

    enum CodingKeys: String, CodingKey {
        case jobID = "job_id"
        case startRequestID = "start_request_id"
        case resourceID = "resource_id"
        case state
    }

    public init(jobID: String, startRequestID: String, resourceID: String, state: ProviderSetupState) {
        self.jobID = jobID
        self.startRequestID = startRequestID
        self.resourceID = resourceID
        self.state = state
    }
}

public struct ProviderRuntimeResource: Decodable, Equatable, Sendable {
    public let resourceID: String
    public let displayName: String
    public let kind: ProviderRuntimeResourceKind
    public let version: String?
    public let source: ProviderRuntimeResourceSource
    public let downloadHost: String?
    public let sha256: String?
    public let license: String

    enum CodingKeys: String, CodingKey {
        case resourceID = "resource_id"
        case displayName = "display_name"
        case kind, version, source
        case downloadHost = "download_host"
        case sha256, license
    }

    public init(
        resourceID: String, displayName: String, kind: ProviderRuntimeResourceKind,
        version: String?, source: ProviderRuntimeResourceSource, downloadHost: String?,
        sha256: String?, license: String
    ) {
        self.resourceID = resourceID
        self.displayName = displayName
        self.kind = kind
        self.version = version
        self.source = source
        self.downloadHost = downloadHost
        self.sha256 = sha256
        self.license = license
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        resourceID = try container.decode(String.self, forKey: .resourceID)
        displayName = try container.decode(String.self, forKey: .displayName)
        kind = try container.decode(ProviderRuntimeResourceKind.self, forKey: .kind)
        version = try container.decode(String?.self, forKey: .version)
        source = try container.decode(ProviderRuntimeResourceSource.self, forKey: .source)
        downloadHost = try container.decode(String?.self, forKey: .downloadHost)
        sha256 = try container.decode(String?.self, forKey: .sha256)
        license = try container.decode(String.self, forKey: .license)
        if source == .approvedDownload {
            guard version?.isEmpty == false, downloadHost?.isEmpty == false,
                let sha256, sha256.count == 64,
                sha256.allSatisfy({ "0123456789abcdef".contains($0) })
            else {
                throw DecodingError.dataCorruptedError(
                    forKey: .source, in: container,
                    debugDescription: "Approved runtime downloads require verified metadata")
            }
        } else if downloadHost != nil {
            throw DecodingError.dataCorruptedError(
                forKey: .downloadHost, in: container,
                debugDescription: "Non-download resources must not have a download host")
        }
    }
}

public struct ProviderRuntime: Decodable, Equatable, Sendable {
    public let state: ProviderRuntimeState
    public let version: String?
    public let catalogRevision: String?
    public let resources: [ProviderRuntimeResource]
    public let activeSetup: ProviderSetupOperation?
    public let lastSetup: ProviderSetupOperation?

    enum CodingKeys: String, CodingKey {
        case state, version
        case catalogRevision = "catalog_revision"
        case resources
        case activeSetup = "active_setup"
        case lastSetup = "last_setup"
    }

    public init(
        state: ProviderRuntimeState, version: String?, catalogRevision: String?,
        resources: [ProviderRuntimeResource], activeSetup: ProviderSetupOperation?,
        lastSetup: ProviderSetupOperation?
    ) {
        self.state = state
        self.version = version
        self.catalogRevision = catalogRevision
        self.resources = resources
        self.activeSetup = activeSetup
        self.lastSetup = lastSetup
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        state = try container.decode(ProviderRuntimeState.self, forKey: .state)
        version = try container.decode(String?.self, forKey: .version)
        catalogRevision = try container.decode(String?.self, forKey: .catalogRevision)
        resources = try container.decode([ProviderRuntimeResource].self, forKey: .resources)
        activeSetup = try container.decode(ProviderSetupOperation?.self, forKey: .activeSetup)
        lastSetup = try container.decode(ProviderSetupOperation?.self, forKey: .lastSetup)
    }
}

/// Installed adapter metadata does not imply authenticated access or generation support.
public struct ProviderDescriptor: Decodable, Equatable, Sendable {
    public let providerID: ProviderID
    public let displayName: String
    public let roles: [ProviderRole]
    public let authMethods: [ProviderAuthMethod]
    public let availability: ProviderAvailability
    public let reason: String?
    public let runtime: ProviderRuntime

    enum CodingKeys: String, CodingKey {
        case providerID = "provider_id"
        case displayName = "display_name"
        case roles
        case authMethods = "auth_methods"
        case availability, reason, runtime
    }

    public init(
        providerID: ProviderID, displayName: String, roles: [ProviderRole],
        authMethods: [ProviderAuthMethod], availability: ProviderAvailability,
        reason: String?, runtime: ProviderRuntime
    ) {
        self.providerID = providerID
        self.displayName = displayName
        self.roles = roles
        self.authMethods = authMethods
        self.availability = availability
        self.reason = reason
        self.runtime = runtime
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        providerID = try container.decode(ProviderID.self, forKey: .providerID)
        displayName = try container.decode(String.self, forKey: .displayName)
        roles = try container.decode([ProviderRole].self, forKey: .roles)
        authMethods = try container.decode([ProviderAuthMethod].self, forKey: .authMethods)
        availability = try container.decode(ProviderAvailability.self, forKey: .availability)
        reason = try container.decode(String?.self, forKey: .reason)
        runtime = try container.decode(ProviderRuntime.self, forKey: .runtime)
        guard !roles.isEmpty, Set(roles).count == roles.count,
            authMethods == [providerID.supportedAuthMethod]
        else {
            throw DecodingError.dataCorruptedError(
                forKey: .authMethods, in: container,
                debugDescription: "Provider capabilities must match the supported contract")
        }
    }
}
