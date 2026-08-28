import Foundation
import Observation

@MainActor
@Observable
public final class ProviderSettingsStore {
    public enum Operation: Equatable, Sendable {
        case loading, saving, reconcilingSave, deleting, testing, loadingModels
        case authenticating, checkingAuthentication, preparing, checkingSetup, cancelling

        public var label: String {
            switch self {
            case .loading: return "設定を読み込み中…"
            case .saving: return "接続設定を保存中…"
            case .reconcilingSave: return "前回と同じ保存要求を確認中…"
            case .deleting: return "接続を削除中…"
            case .testing: return "接続とメタデータを確認中…"
            case .loadingModels: return "モデル候補を読み込み中…"
            case .authenticating: return "認証操作を受付中…"
            case .checkingAuthentication: return "認証操作の状態を確認中…"
            case .preparing: return "実行環境の準備を受付中…"
            case .checkingSetup: return "準備ジョブの状態を確認中…"
            case .cancelling: return "取消を要求中…"
            }
        }
    }

    public internal(set) var providers: [ProviderDescriptor] = []
    public internal(set) var connections: [ProviderConnection] = []
    public internal(set) var selectedConnectionID: String?
    public var editor = ProviderConnectionSettings()
    public internal(set) var models: [ProviderModelDescriptor] = []
    public internal(set) var catalogState: ProviderCatalogState?
    public internal(set) var nextModelCursor: String?
    public internal(set) var catalogFetchedAt: String?
    public internal(set) var lastTest: ProviderConnectionTestResult?
    public internal(set) var recovery = ProviderSettingsRecovery()
    public internal(set) var setupJobs: [ProviderID: Job] = [:]
    public internal(set) var isLoaded = false
    public internal(set) var operation: Operation?
    public internal(set) var errorMessage: String?
    public internal(set) var notice: String?
    public internal(set) var revisionConflict = false

    @ObservationIgnored let client: any ProviderSettingsClient
    @ObservationIgnored var generation: UInt64 = 0
    var pendingSave: ProviderSettingsSaveRecovery?
    var recoverySnapshotAvailable = false

    public init(client: any ProviderSettingsClient) { self.client = client }

    public var isBusy: Bool { operation != nil }
    public var canEdit: Bool { isLoaded && !isBusy }
    public var saveNeedsReconciliation: Bool { pendingSave != nil }
    public var canAddConnection: Bool { canEdit && !saveNeedsReconciliation }
    public var selectedConnection: ProviderConnection? {
        connections.first { $0.connectionID == selectedConnectionID }
    }
    public var selectedProvider: ProviderDescriptor? {
        providers.first { $0.providerID == editor.providerID }
    }
    public var pendingAuthentication: ProviderSettingsRecovery.Authentication? {
        selectedConnectionID.flatMap { recovery.authentications[$0] }
    }
    public var canSave: Bool {
        canEdit && !saveNeedsReconciliation && !revisionConflict && editor.canSave && pendingAuthentication?.unresolved != true
            && selectedConnection?.activeAuth == nil
    }
    public var canUseSavedConnection: Bool {
        canEdit && !saveNeedsReconciliation && !editor.hasUnsavedChanges && !revisionConflict && selectedConnection?.enabled == true
    }
    public var canTest: Bool {
        canUseSavedConnection && pendingAuthentication?.unresolved != true
            && (selectedConnection?.providerID != .codexAppServer || selectedProvider?.runtime.state == .ready)
            && (selectedConnection?.authMethod == ProviderAuthMethod.none || selectedConnection?.credentialPresent == true)
    }
    public var canAuthenticate: Bool {
        guard canUseSavedConnection, pendingAuthentication?.unresolved != true else { return false }
        if selectedConnection?.authMethod == .chatgpt { return selectedProvider?.runtime.state == .ready }
        return canTest
    }
    public var canLogout: Bool {
        guard canEdit, !saveNeedsReconciliation, !editor.hasUnsavedChanges, !revisionConflict,
            let connection = selectedConnection, pendingAuthentication?.unresolved != true else { return false }
        // Codex invalidates credential presence when logout is accepted. A failed
        // cleanup must remain explicitly retryable even after reopening settings.
        if connection.providerID == .codexAppServer { return true }
        return connection.enabled && connection.authMethod != .none && connection.credentialPresent
    }
    /// A challenge is presented only for the currently selected, matching login.
    public var deviceAuthorization: ProviderDeviceAuthorization? {
        guard isLoaded, !revisionConflict,
            let connection = selectedConnection, connection.enabled,
            connection.providerID == .codexAppServer, connection.authMethod == .chatgpt,
            let pending = pendingAuthentication, pending.state == .pending, pending.action == .start,
            pending.connectionRevision == connection.revision,
            let active = connection.activeAuth, active.state == .pending,
            active.operationID == pending.operationID, active.startRequestID == pending.startRequestID,
            active.serverInstanceID == pending.serverInstanceID,
            let authorizationURL = pending.authorizationURL, let userCode = pending.userCode else { return nil }
        return ProviderDeviceAuthorization(authorizationURL: authorizationURL, userCode: userCode)
    }
    public var browserAuthorizationURL: URL? { deviceAuthorization?.authorizationURL.browserURL }

    /// Invoked by the copy button only. The writer is injected so tests never touch the clipboard.
    public func copyAuthorizationUserCode(_ writer: (String) -> Bool) {
        guard canEdit, let authorization = deviceAuthorization else { return }
        notice = nil
        errorMessage = nil
        if writer(authorization.userCode.displayValue) {
            notice = "確認コードをコピーしました。公式のデバイスログイン画面に入力してください。"
        } else {
            errorMessage = "確認コードをコピーできませんでした。表示されているコードを公式画面に入力してください。"
        }
    }
    public var canDelete: Bool {
        canEdit && !saveNeedsReconciliation && selectedConnection != nil && pendingAuthentication?.unresolved != true
    }
    public var needsPolling: Bool { recovery.needsPolling }

    public func load(discardEdits: Bool = false) async {
        guard let token = begin(.loading) else { return }
        if discardEdits { editor.clearSensitiveInput() }
        do {
            try await refreshSnapshot(token: token, discardEdits: discardEdits)
            finish(token)
        } catch { fail(error, token: token) }
    }

    public func selectConnection(_ connectionID: String) {
        guard canEdit, selectedConnectionID != connectionID,
            let connection = connections.first(where: { $0.connectionID == connectionID }) else { return }
        selectedConnectionID = connectionID
        editor = ProviderConnectionSettings(connection: connection)
        clearSelectionFeedback()
    }

    public func newConnection() {
        guard canAddConnection else { return }
        selectedConnectionID = nil
        editor = ProviderConnectionSettings(providerID: providers.first?.providerID ?? .anthropicAPI)
        clearSelectionFeedback()
    }

    public func selectProvider(_ providerID: ProviderID) {
        guard canEdit, providers.contains(where: { $0.providerID == providerID }) else { return }
        editor.selectProvider(providerID)
    }

    public func save() async {
        guard canSave else { return }
        guard let request = editor.takeSaveRequest(), let token = begin(.saving) else { return }
        let attempt = ProviderSettingsSaveRecovery(editor: editor, connections: connections, request: request)
        // Keep only the non-secret receipt metadata before the first suspension.
        // A disappearing window can invalidate callbacks without proving non-delivery.
        pendingSave = attempt
        recoverySnapshotAvailable = false
        lastTest = nil
        do {
            let response = try await client.setProviderConnection(request)
            guard isCurrent(token) else { return }
            if let existing = editor.connection {
                guard response.connection.connectionID == existing.connectionID,
                    response.connection.providerID == existing.providerID else {
                    throw ProviderSettingsFailure(.protocolError)
                }
            } else if response.connection.providerID != editor.providerID {
                throw ProviderSettingsFailure(.protocolError)
            }
            upsert(response.connection)
            selectedConnectionID = response.connection.connectionID
            editor.adopt(response.connection)
            pendingSave = nil
            clearModels()
            notice = "接続設定を保存しました。この操作では認証・議事録生成を実行していません。"
            finish(token)
        } catch {
            guard isCurrent(token) else { return }
            let code = (error as? ProviderSettingsFailure)?.code ?? .internalError
            if code.rejectsBeforeAcceptance { pendingSave = nil }
            fail(error, token: token, saving: true)
        }
    }

    /// The caller presents an explicit deletion confirmation before invoking this method.
    public func deleteConnection() async {
        guard canDelete, let connection = selectedConnection, let token = begin(.deleting) else { return }
        editor.clearSensitiveInput()
        do {
            let response = try await client.deleteProviderConnection(DeleteProviderConnectionRequest(
                connectionID: connection.connectionID, expectedRevision: connection.revision, confirm: true))
            guard isCurrent(token) else { return }
            guard response.connectionID == connection.connectionID, response.deleted else {
                throw ProviderSettingsFailure(.protocolError)
            }
            connections.removeAll { $0.connectionID == connection.connectionID }
            chooseInitialConnection()
            clearModels()
            lastTest = nil
            notice = "接続と、その接続専用の認証情報を削除しました。過去の議事録は保持します。"
            finish(token)
        } catch { fail(error, token: token) }
    }

    public func testConnection() async {
        guard canTest, let connection = selectedConnection, let token = begin(.testing) else { return }
        lastTest = nil
        do {
            let result = try await client.testProviderConnection(TestProviderConnectionRequest(
                connectionID: connection.connectionID, expectedRevision: connection.revision))
            guard isCurrent(token) else { return }
            guard result.connection.connectionID == connection.connectionID,
                result.connection.revision == connection.revision else {
                throw ProviderSettingsFailure(.configurationConflict)
            }
            upsert(result.connection)
            editor.adopt(result.connection)
            lastTest = result
            notice = result.connected
                ? "接続とメタデータを確認しました。議事録生成は未検証です。"
                : "接続を確認できませんでした。認証・実行環境の状態を確認してください。"
            finish(token)
        } catch { fail(error, token: token) }
    }

    public func loadModels(refresh: Bool = false, append: Bool = false) async {
        guard canEdit, let connection = selectedConnection,
            !refresh || canTest, !append || nextModelCursor != nil,
            let token = begin(.loadingModels) else { return }
        let cursor = append ? nextModelCursor : nil
        do {
            let response = try await client.listProviderModels(ListProviderModelsRequest(
                connectionID: connection.connectionID, cursor: cursor, refresh: refresh))
            guard isCurrent(token) else { return }
            guard response.connectionID == connection.connectionID,
                response.connectionRevision == connection.revision else {
                throw ProviderSettingsFailure(.configurationConflict)
            }
            if append {
                let known = Set(models.map(\.modelID))
                models.append(contentsOf: response.models.filter { !known.contains($0.modelID) })
            } else {
                models = response.models
            }
            nextModelCursor = response.nextCursor == cursor ? nil : response.nextCursor
            catalogState = response.catalogState
            catalogFetchedAt = response.fetchedAt
            finish(token)
        } catch { fail(error, token: token) }
    }

    public func dismiss() {
        generation &+= 1
        operation = nil
        isLoaded = false
        recoverySnapshotAvailable = false
        editor.dismiss()
        recovery.clearAuthorizationChallenges()
        clearModels()
        lastTest = nil
        errorMessage = nil
        notice = nil
    }

    func begin(_ operation: Operation) -> UInt64? {
        guard !isBusy else { return nil }
        generation &+= 1
        self.operation = operation
        errorMessage = nil
        notice = nil
        return generation
    }

    func isCurrent(_ token: UInt64) -> Bool { generation == token && operation != nil }

    func finish(_ token: UInt64) {
        guard isCurrent(token) else { return }
        operation = nil
    }

    func fail(_ error: Error, token: UInt64, saving: Bool = false) {
        guard isCurrent(token) else { return }
        editor.apiKey = ""
        let failure = error as? ProviderSettingsFailure ?? ProviderSettingsFailure(.internalError)
        errorMessage = failure.message
        if saving && editor.usesAPIKey { errorMessage! += " API キーの入力は消去しました。必要なら再入力してください。" }
        if failure.code == .configurationConflict { revisionConflict = true }
        operation = nil
    }

    func refreshSnapshot(token: UInt64, discardEdits: Bool = false) async throws {
        async let providerResponse = client.listProviders()
        async let connectionResponse = client.listProviderConnections()
        let snapshot = try await (providerResponse, connectionResponse)
        guard isCurrent(token) else { return }
        providers = snapshot.0.providers
        connections = snapshot.1.connections
        recovery.observe(providers: providers)
        recovery.observe(connections: connections)
        setupJobs = setupJobs.filter {
            recovery.setups[$0.key]?.jobID == $0.value.jobID
                && recovery.setups[$0.key]?.state.rawValue == $0.value.status
        }
        reconcileSavedSnapshot()
        if let current = selectedConnection {
            if editor.connection?.revision != current.revision {
                clearModels()
                lastTest = nil
            }
            if discardEdits || !editor.hasUnsavedChanges {
                editor.adopt(current)
                revisionConflict = false
            } else if editor.connection?.revision != current.revision {
                revisionConflict = true
            } else {
                editor.synchronizeStatus(current)
            }
        } else if !isLoaded || selectedConnectionID != nil || discardEdits {
            chooseInitialConnection()
        }
        isLoaded = true
    }

    func upsert(_ connection: ProviderConnection) {
        if let index = connections.firstIndex(where: { $0.connectionID == connection.connectionID }) {
            connections[index] = connection
        } else { connections.append(connection) }
    }

    func chooseInitialConnection() {
        clearModels()
        lastTest = nil
        if let first = connections.first {
            selectedConnectionID = first.connectionID
            editor = ProviderConnectionSettings(connection: first)
        } else {
            selectedConnectionID = nil
            editor = ProviderConnectionSettings(providerID: providers.first?.providerID ?? .anthropicAPI)
        }
        revisionConflict = false
    }

    func clearSelectionFeedback() {
        clearModels()
        lastTest = nil
        errorMessage = nil
        notice = nil
        revisionConflict = false
    }

    func clearModels() {
        models = []
        catalogState = nil
        nextModelCursor = nil
        catalogFetchedAt = nil
    }
}
