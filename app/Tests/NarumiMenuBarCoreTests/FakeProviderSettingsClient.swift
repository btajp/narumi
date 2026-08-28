import Foundation
@testable import NarumiMenuBarCore

actor FakeProviderSettingsClient: ProviderSettingsClient {
    struct Scenario: Sendable {
        var loseAuthResponse = false
        var authLookupState: ProviderAuthOperationState = .succeeded
        var authLookupFails = false
        var loseSetupResponse = false
        var setupCompletesBeforeResponse = false
        var saveFailure: ProviderSettingsFailure?
        var saveRawFailure = false
        var holdSave = false
        var modelRevision: Int?
        var authRejection: ProviderSettingsFailure?
        var setupRejection: ProviderSettingsFailure?
        var loseSaveResponse = false
        var failKeychainAfterMetadata = false
    }

    var connections: [ProviderConnection]
    var providers: [ProviderDescriptor]
    var scenario: Scenario
    private(set) var saveRequests: [SetProviderConnectionRequest] = []
    private(set) var saveMutationCount = 0
    private(set) var authRequests: [AuthenticateProviderConnectionRequest] = []
    private(set) var authLookups: [GetProviderAuthStatusRequest] = []
    private(set) var setupRequests: [PrepareProviderRuntimeRequest] = []
    private(set) var modelRequests: [ListProviderModelsRequest] = []
    private(set) var testRequests: [TestProviderConnectionRequest] = []
    private(set) var cancelledJobs: [String] = []
    private var authOperation: ProviderAuthOperation?
    private var savedSetup: ProviderSetupOperation?
    private var saveStarted: CheckedContinuation<Void, Never>?
    private var saveRelease: CheckedContinuation<Void, Never>?
    private var saveReceipts: [String: (SetProviderConnectionRequest, ProviderConnectionResponse)] = [:]
    private var failedSaves: [String: SetProviderConnectionRequest] = [:]

    init(
        connections: [ProviderConnection] = [ProviderSettingsFixtures.connection()],
        providers: [ProviderDescriptor] = [ProviderSettingsFixtures.provider()], scenario: Scenario = Scenario()
    ) {
        self.connections = connections
        self.providers = providers
        self.scenario = scenario
        if let active = connections.first?.activeAuth {
            authOperation = ProviderSettingsFixtures.auth(requestID: active.startRequestID, state: active.state)
        }
        savedSetup = providers.first?.runtime.activeSetup ?? providers.first?.runtime.lastSetup
    }

    func listProviders() async throws -> ListProvidersResponse { ListProvidersResponse(providers: providers) }
    func listProviderConnections() async throws -> ListProviderConnectionsResponse { ListProviderConnectionsResponse(connections: connections) }

    func replaceConnections(_ connections: [ProviderConnection]) { self.connections = connections }
    func configure(_ scenario: Scenario) { self.scenario = scenario }

    func setProviderConnection(_ request: SetProviderConnectionRequest) async throws -> ProviderConnectionResponse {
        saveRequests.append(request)
        saveStarted?.resume()
        saveStarted = nil
        if let receipt = saveReceipts[request.requestID] {
            guard receipt.0 == request else { throw ProviderSettingsFailure(.configurationConflict) }
            return receipt.1
        }
        if let failed = failedSaves[request.requestID] {
            guard failed == request else { throw ProviderSettingsFailure(.configurationConflict) }
            throw ProviderSettingsFailure(.internalError)
        }
        if scenario.holdSave { await withCheckedContinuation { saveRelease = $0 } }
        if scenario.saveRawFailure { throw SensitiveFixtureError() }
        if let failure = scenario.saveFailure { throw failure }
        let existing: ProviderConnection
        if let id = request.connectionID {
            guard let found = connections.first(where: { $0.connectionID == id }) else {
                throw ProviderSettingsFailure(.notFound)
            }
            guard found.revision == request.expectedRevision else { throw ProviderSettingsFailure(.configurationConflict) }
            existing = found
        } else {
            existing = ProviderSettingsFixtures.connection(
                connectionID: "conn-" + UUID().uuidString.replacingOccurrences(of: "-", with: "").lowercased(),
                revision: 0, providerID: request.providerID ?? .anthropicAPI, credential: false)
        }
        let hasKey: Bool
        switch request.apiKey {
        case .unchanged: hasKey = existing.credentialPresent
        case .clear: hasKey = false
        case .replace: hasKey = true
        }
        let actualCredential = hasKey && !scenario.failKeychainAfterMetadata
        let connection = ProviderSettingsFixtures.changed(
            existing, revision: existing.revision + 1, name: request.displayName,
            enabled: request.enabled, credential: actualCredential, authState: actualCredential ? .unverified : .unconfigured)
        if let index = connections.firstIndex(where: { $0.connectionID == connection.connectionID }) {
            connections[index] = connection
        } else { connections.append(connection) }
        saveMutationCount += 1
        if scenario.failKeychainAfterMetadata {
            failedSaves[request.requestID] = request
            throw ProviderSettingsFailure(.internalError)
        }
        let response = ProviderConnectionResponse(connection: connection)
        saveReceipts[request.requestID] = (request, response)
        if scenario.loseSaveResponse { throw ProviderSettingsFailure(.transport) }
        return response
    }

    func waitForSaveStart() async {
        if !saveRequests.isEmpty { return }
        await withCheckedContinuation { saveStarted = $0 }
    }

    func releaseSave() {
        saveRelease?.resume()
        saveRelease = nil
    }

    func deleteProviderConnection(_ request: DeleteProviderConnectionRequest) async throws -> DeleteProviderConnectionResponse {
        connections.removeAll { $0.connectionID == request.connectionID }
        return DeleteProviderConnectionResponse(connectionID: request.connectionID, deleted: true)
    }

    func authenticateProviderConnection(_ request: AuthenticateProviderConnectionRequest) async throws -> ProviderAuthResponse {
        authRequests.append(request)
        if let rejection = scenario.authRejection { throw rejection }
        if request.action == .cancel, let previous = authOperation {
            authOperation = ProviderSettingsFixtures.auth(
                requestID: previous.startRequestID, state: .cancelled, action: previous.action)
        } else {
            authOperation = ProviderSettingsFixtures.auth(requestID: request.requestID, action: request.action)
        }
        let operation = authOperation!
        applyAuthState(operation)
        if scenario.loseAuthResponse { throw ProviderSettingsFailure(.transport) }
        return ProviderAuthResponse(operation: operation)
    }

    func providerAuthStatus(_ request: GetProviderAuthStatusRequest) async throws -> ProviderAuthResponse {
        authLookups.append(request)
        if scenario.authLookupFails { throw ProviderSettingsFailure(.transport) }
        guard let previous = authOperation, request.startRequestID == previous.startRequestID else {
            throw ProviderSettingsFailure(.notFound)
        }
        let operation = ProviderSettingsFixtures.auth(
            requestID: previous.startRequestID, state: scenario.authLookupState, action: previous.action)
        authOperation = operation
        applyAuthState(operation)
        return ProviderAuthResponse(operation: operation)
    }

    func testProviderConnection(_ request: TestProviderConnectionRequest) async throws -> ProviderConnectionTestResult {
        testRequests.append(request)
        let connection = ProviderSettingsFixtures.changed(connections[0], authState: .authenticated, catalogState: .ready)
        connections = [connection]
        return ProviderConnectionTestResult(connection: connection, connected: true, reason: nil)
    }

    func listProviderModels(_ request: ListProviderModelsRequest) async throws -> ListProviderModelsResponse {
        modelRequests.append(request)
        return ListProviderModelsResponse(
            connectionID: request.connectionID, connectionRevision: scenario.modelRevision ?? connections[0].revision,
            models: [ProviderSettingsFixtures.model()], nextCursor: nil, catalogState: .ready,
            fetchedAt: ProviderSettingsFixtures.timestamp)
    }

    func prepareProviderRuntime(_ request: PrepareProviderRuntimeRequest) async throws -> PrepareProviderRuntimeResponse {
        setupRequests.append(request)
        if let rejection = scenario.setupRejection { throw rejection }
        let state: ProviderSetupState = scenario.setupCompletesBeforeResponse ? .succeeded : .running
        savedSetup = ProviderSettingsFixtures.setup(requestID: request.requestID, state: state)
        applySetupState(providerID: request.providerID)
        if scenario.loseSetupResponse { throw ProviderSettingsFailure(.transport) }
        return PrepareProviderRuntimeResponse(jobID: ProviderSettingsFixtures.jobID)
    }

    func jobStatus(jobID: String) async throws -> Job {
        guard jobID == savedSetup?.jobID else { throw ProviderSettingsFailure(.notFound) }
        return ProviderSettingsFixtures.job(state: savedSetup!.state.rawValue)
    }

    func cancelJob(jobID: String) async throws -> Job {
        cancelledJobs.append(jobID)
        guard let setup = savedSetup else { throw ProviderSettingsFailure(.notFound) }
        savedSetup = ProviderSettingsFixtures.setup(requestID: setup.startRequestID, state: .cancelled)
        applySetupState(providerID: providers[0].providerID)
        return ProviderSettingsFixtures.job(state: "cancelled")
    }

    private func applyAuthState(_ operation: ProviderAuthOperation) {
        guard let connection = connections.first else { return }
        let active: ProviderActiveAuth? = operation.state == .pending || operation.state == .unknown
            ? ProviderSettingsFixtures.activeAuth(requestID: operation.startRequestID, state: operation.state) : nil
        let authState: ProviderAuthState = operation.state == .succeeded ? .authenticated : .authenticating
        connections = [ProviderSettingsFixtures.changed(
            connection, credential: operation.action == .logout && operation.state == .succeeded ? false : nil,
            authState: authState, activeAuth: active)]
    }

    private func applySetupState(providerID: ProviderID) {
        guard let setup = savedSetup else { return }
        let active = setup.state == .running || setup.state == .queued
        providers = [ProviderSettingsFixtures.provider(
            providerID: providerID, state: active ? .preparing : .ready,
            activeSetup: active ? setup : nil, lastSetup: setup)]
    }

    private struct SensitiveFixtureError: LocalizedError {
        var errorDescription: String? { "upstream echo: not-a-real-key" }
    }
}
