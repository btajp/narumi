import Foundation
import Observation

/// This surface cannot authenticate, change a connection, or generate a meeting.
/// Its only generation request is the separately confirmed fixed model-verification prompt.
public protocol MinutesModelCatalogClient: Sendable {
    func listProviders() async throws -> ListProvidersResponse
    func listProviderConnections() async throws -> ListProviderConnectionsResponse
    func listProviderModels(_ request: ListProviderModelsRequest) async throws -> ListProviderModelsResponse
    func verifyProviderModel(_ request: VerifyProviderModelRequest) async throws -> VerifyProviderModelResponse
}

public extension MinutesModelCatalogClient {
    func verifyProviderModel(_: VerifyProviderModelRequest) async throws -> VerifyProviderModelResponse {
        throw ProviderSettingsFailure(.unsupported)
    }
}

@MainActor
@Observable
public final class MinutesModelCatalogStore {
    private struct VerificationAttempt: Equatable {
        let request: VerifyProviderModelRequest
        let model: ProviderModelDescriptor

        func matches(
            connectionID: String, expectedRevision: Int, model: ProviderModelDescriptor,
            confirmation: ProviderModelVerificationConfirmation = .sendTestPromptAndMayCharge
        ) -> Bool {
            request.connectionID == connectionID
                && request.expectedRevision == expectedRevision
                && request.modelID == model.modelID
                && request.confirmation == confirmation
                && self.model == model
        }

        func isResolved(by catalog: ListProviderModelsResponse) -> Bool {
            guard catalog.connectionID == request.connectionID,
                catalog.connectionRevision == request.expectedRevision,
                catalog.catalogState == .ready,
                let current = catalog.models.first(where: { $0.modelID == request.modelID }),
                current.availability == .available else { return false }
            // Probe promotion changes capabilities and availability. These fields are the
            // discovery identity established before the paid request and must remain fixed.
            return current.displayName == model.displayName
                && current.resolvedRevision == model.resolvedRevision
                && current.timestampSupport == model.timestampSupport
                && current.availabilityExpiresOn == model.availabilityExpiresOn
                && current.source == model.source
                && current.billing.kind == model.billing.kind
        }

        var recoveryNotice: String {
            "モデル「\(model.modelID)」の検証結果は未確定です。同じ要求 ID \(request.requestID) と候補を再確認に使います。別の要求 ID では送信しません。"
        }
    }

    public private(set) var connections: [ProviderConnection] = []
    public private(set) var providers: [ProviderDescriptor] = []
    public private(set) var supportedProviders: [String]
    public private(set) var modelVerificationSupported: Bool
    public private(set) var catalogs: [String: ListProviderModelsResponse] = [:]
    public private(set) var isLoading = false
    public private(set) var errorMessage: String?
    public private(set) var verificationNotice: String?
    @ObservationIgnored private let client: any MinutesModelCatalogClient
    @ObservationIgnored private var generation: UInt64 = 0
    /// Retained across metadata reloads and server reconnects until the exact paid request is resolved.
    @ObservationIgnored private var unresolvedVerification: VerificationAttempt?

    public init(
        client: any MinutesModelCatalogClient, supportedProviders: [String] = [],
        modelVerificationSupported: Bool = false
    ) {
        self.client = client
        self.supportedProviders = MinutesModelSelection.providers.filter { supportedProviders.contains($0) }
        self.modelVerificationSupported = modelVerificationSupported
    }

    public func setSupportedProviders(
        _ values: [String], modelVerificationSupported: Bool = false
    ) {
        let next = MinutesModelSelection.providers.filter { values.contains($0) }
        guard next != supportedProviders || modelVerificationSupported != self.modelVerificationSupported else { return }
        generation &+= 1
        isLoading = false
        supportedProviders = next
        self.modelVerificationSupported = modelVerificationSupported
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
            return "このプロバイダの議事録生成能力はサーバーから公開されていません。"
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
        verificationNotice = unresolvedVerification?.recoveryNotice
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
        verificationNotice = unresolvedVerification?.recoveryNotice
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
        verificationNotice = unresolvedVerification?.recoveryNotice
        defer { finish(token) }
        do {
            try await fetchModels(connection: selected, refresh: false, cursor: cursor, token: token)
            finish(token)
        } catch { fail(error, connectionID: connectionID, token: token) }
    }

    public func isVerificationCandidate(
        connectionID: String, expectedRevision: Int, model: ProviderModelDescriptor
    ) -> Bool {
        guard modelVerificationSupported,
            let selected = connection(connectionID), selected.enabled,
            selected.revision == expectedRevision,
            [.claudeAgentSDK, .openAICompatibleAPI].contains(selected.providerID),
            connectionUnavailableReason(selected) == nil,
            let catalog = catalogs[connectionID], catalog.catalogState == .ready,
            let current = catalog.models.first(where: { $0.modelID == model.modelID }),
            current == model else { return false }
        if let unresolvedVerification {
            guard unresolvedVerification.matches(
                connectionID: connectionID, expectedRevision: expectedRevision, model: model) else { return false }
        }
        return MinutesModelForm.isModelVerificationCandidate(model, provider: selected.providerID.rawValue)
    }

    public func rejectChangedVerificationConfirmation() {
        if unresolvedVerification != nil {
            verificationNotice = "前回のモデル検証結果が未確定のため、別の接続・モデルへのテスト文は送信しませんでした。元の候補で結果を確認してください。"
        } else {
            verificationNotice = "確認中に接続またはモデル候補が変更されたため、テスト文は送信しませんでした。最新の候補を確認してください。"
        }
    }

    /// Sends one fixed, non-meeting test prompt only after the UI obtains explicit confirmation.
    /// Failure never retries automatically because the provider may already have processed and billed the request.
    public func verifyModel(
        connectionID: String, expectedRevision: Int, expectedModel: ProviderModelDescriptor,
        confirmation: ProviderModelVerificationConfirmation
    ) async {
        guard !isLoading else { return }
        guard confirmation == .sendTestPromptAndMayCharge,
            isVerificationCandidate(
                connectionID: connectionID, expectedRevision: expectedRevision, model: expectedModel),
            let selected = connection(connectionID) else {
            rejectChangedVerificationConfirmation()
            return
        }
        let attempt: VerificationAttempt
        if let unresolvedVerification {
            guard unresolvedVerification.matches(
                connectionID: connectionID, expectedRevision: expectedRevision, model: expectedModel,
                confirmation: confirmation) else {
                rejectChangedVerificationConfirmation()
                return
            }
            attempt = unresolvedVerification
        } else {
            attempt = VerificationAttempt(
                request: VerifyProviderModelRequest(
                    connectionID: connectionID, expectedRevision: selected.revision,
                    modelID: expectedModel.modelID, confirmation: confirmation),
                model: expectedModel)
            // Store the immutable request before the first suspension. A transport failure
            // cannot prove that the provider did not process or bill the fixed prompt.
            unresolvedVerification = attempt
        }
        generation &+= 1
        let token = generation
        isLoading = true
        errorMessage = nil
        verificationNotice = unresolvedVerification?.recoveryNotice
        defer { finish(token) }
        do {
            let response = try await client.verifyProviderModel(attempt.request)
            guard response.connectionID == connectionID, response.connectionRevision == selected.revision,
                response.model.modelID == expectedModel.modelID else {
                throw ProviderSettingsFailure(.protocolError)
            }
            if unresolvedVerification == attempt {
                unresolvedVerification = nil
                verificationNotice = nil
            }
            guard isCurrent(token) else { return }
            let verifiedCatalog = try adoptVerifiedModel(response, expectedModel: attempt.model)
            do {
                try await fetchModels(connection: selected, refresh: false, cursor: nil, token: token)
                guard isCurrent(token) else { return }
                try reconcileVerifiedModel(response, previousCatalog: verifiedCatalog)
            } catch {
                guard isCurrent(token) else { return }
                catalogs[response.connectionID] = verifiedCatalog
                verificationNotice = "モデルの検証は完了しました。候補一覧の再読み込みだけ失敗したため、同じモデルを再検証する必要はありません。"
            }
            finish(token)
        } catch {
            let failure = error as? ProviderSettingsFailure ?? ProviderSettingsFailure(.internalError)
            let rejectedBeforeProbe = Self.verificationRejectedBeforeProbe(failure.code)
            let knownProbeFailure = failure.code == .engineUnavailable
                && failure.modelVerification?.isKnownFailure == true
            if (rejectedBeforeProbe || knownProbeFailure), unresolvedVerification == attempt {
                unresolvedVerification = nil
            }
            guard isCurrent(token) else { return }
            errorMessage = failure.message
            if knownProbeFailure {
                verificationNotice = "モデルの固定テストは確定的に失敗しました。次の明示確認では新しい要求 ID で再検証できます。"
            } else if rejectedBeforeProbe {
                verificationNotice = nil
            } else {
                verificationNotice = attempt.recoveryNotice
            }
            isLoading = false
        }
    }

    public func invalidate() {
        generation &+= 1
        connections = []
        providers = []
        supportedProviders = []
        catalogs = [:]
        isLoading = false
        errorMessage = nil
        verificationNotice = unresolvedVerification?.recoveryNotice
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
        let catalog = ListProviderModelsResponse(
            connectionID: response.connectionID, connectionRevision: response.connectionRevision,
            models: models, nextCursor: response.nextCursor == cursor ? nil : response.nextCursor,
            catalogState: response.catalogState, fetchedAt: response.fetchedAt)
        catalogs[connection.connectionID] = catalog
        if unresolvedVerification?.isResolved(by: catalog) == true {
            unresolvedVerification = nil
            verificationNotice = "前回のモデル検証が完了していることを、保存済み候補一覧から確認しました。"
        }
    }

    private func adoptVerifiedModel(
        _ response: VerifyProviderModelResponse, expectedModel: ProviderModelDescriptor
    ) throws -> ListProviderModelsResponse {
        guard let catalog = catalogs[response.connectionID],
            catalog.connectionRevision == response.connectionRevision,
            let index = catalog.models.firstIndex(of: expectedModel),
            expectedModel.modelID == response.model.modelID else {
            throw ProviderSettingsFailure(.configurationConflict)
        }
        var models = catalog.models
        models[index] = response.model
        let updated = ListProviderModelsResponse(
            connectionID: catalog.connectionID, connectionRevision: catalog.connectionRevision,
            models: models, nextCursor: catalog.nextCursor,
            catalogState: response.catalogState, fetchedAt: catalog.fetchedAt)
        catalogs[response.connectionID] = updated
        return updated
    }

    private func reconcileVerifiedModel(
        _ response: VerifyProviderModelResponse, previousCatalog: ListProviderModelsResponse
    ) throws {
        guard let catalog = catalogs[response.connectionID],
            catalog.connectionRevision == response.connectionRevision else {
            throw ProviderSettingsFailure(.configurationConflict)
        }
        guard catalog.catalogState == .ready else {
            verificationNotice = "モデル検証は完了しましたが、後から取得した候補一覧が最新ではないため選択可能には戻していません。候補一覧を更新してください。"
            return
        }
        var models = catalog.models
        if let index = models.firstIndex(where: { $0.modelID == response.model.modelID }) {
            switch models[index].availability {
            case .unverified:
                models[index] = response.model
            case .available:
                break
            case .retired, .unsupported, .notPrepared, .authenticationRequired:
                verificationNotice = "モデル検証後に利用不可の候補情報を受信したため、選択可能には戻していません。"
                return
            }
        } else {
            let oldIndex = previousCatalog.models.firstIndex(where: {
                $0.modelID == response.model.modelID
            }) ?? previousCatalog.models.endIndex
            models.insert(response.model, at: min(oldIndex, models.endIndex))
        }
        catalogs[response.connectionID] = ListProviderModelsResponse(
            connectionID: catalog.connectionID, connectionRevision: catalog.connectionRevision,
            models: models, nextCursor: catalog.nextCursor,
            catalogState: catalog.catalogState, fetchedAt: catalog.fetchedAt)
    }

    private func isCurrent(_ token: UInt64) -> Bool { token == generation && !Task.isCancelled }

    /// These codes are emitted by validation before a model probe can be accepted.
    /// configuration_conflict is intentionally absent: runtime/catalog drift can report
    /// that code after the external provider already processed the paid request.
    private static func verificationRejectedBeforeProbe(_ code: ProviderSettingsErrorCode) -> Bool {
        [.invalidArgument, .notFound, .busy, .authenticationRequired].contains(code)
    }

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
