import Foundation

extension ProviderSettingsStore {
    public func startAuthentication() async {
        guard canTest else { return }
        await performAuthentication(.start)
    }

    /// The caller confirms removal of this connection's credential before invoking logout.
    public func logout() async {
        guard canUseSavedConnection, selectedConnection?.credentialPresent == true,
            pendingAuthentication?.unresolved != true else { return }
        await performAuthentication(.logout)
    }

    public func checkAuthentication() async {
        guard canEdit, let pending = pendingAuthentication,
            let token = begin(.checkingAuthentication) else { return }
        do {
            try await readAuthentication(pending, token: token)
            try await refreshSnapshot(token: token)
            finish(token)
        } catch {
            guard isCurrent(token) else { return }
            recovery.authenticationUnconfirmed(connectionID: pending.connectionID)
            fail(error, token: token)
        }
    }

    public func cancelAuthentication() async {
        guard canEdit, let connection = selectedConnection, let pending = pendingAuthentication,
            pending.unresolved, let operationID = pending.operationID,
            let token = begin(.cancelling) else { return }
        do {
            let response = try await client.authenticateProviderConnection(AuthenticateProviderConnectionRequest(
                connectionID: connection.connectionID, expectedRevision: connection.revision,
                action: .cancel, operationID: operationID))
            guard isCurrent(token) else { return }
            guard recovery.receive(response.operation) else { throw ProviderSettingsFailure(.protocolError) }
            try await refreshSnapshot(token: token)
            finish(token)
        } catch {
            guard isCurrent(token) else { return }
            if publicFailure(error).code.rejectsBeforeAcceptance {
                fail(error, token: token)
                return
            }
            recovery.authenticationUnconfirmed(connectionID: connection.connectionID)
            fail(error, token: token)
            await checkAuthentication()
        }
    }

    public func canPrepare(_ resource: ProviderRuntimeResource, provider: ProviderDescriptor) -> Bool {
        guard canEdit, !revisionConflict, provider.runtime.catalogRevision != nil,
            provider.runtime.state != .preparing,
            recovery.setups[provider.providerID]?.unresolved != true,
            provider.runtime.resources.contains(resource) else { return false }
        if resource.source == .approvedDownload {
            return resource.downloadHost != nil && resource.sha256 != nil && resource.version != nil
        }
        return true
    }

    /// Downloads are accepted only after the caller shows the catalog's host and resource.
    public func prepare(_ resource: ProviderRuntimeResource, providerID: ProviderID) async {
        guard let provider = providers.first(where: { $0.providerID == providerID }),
            canPrepare(resource, provider: provider), let revision = provider.runtime.catalogRevision,
            let token = begin(.preparing) else { return }
        let requestID = UUID().uuidString
        guard recovery.beginSetup(providerID: providerID, resourceID: resource.resourceID, requestID: requestID) else {
            finish(token)
            return
        }
        setupJobs.removeValue(forKey: providerID)
        var accepted = false
        do {
            let response = try await client.prepareProviderRuntime(PrepareProviderRuntimeRequest(
                providerID: providerID, resourceID: resource.resourceID,
                expectedCatalogRevision: revision,
                action: provider.runtime.state == .ready ? .update : .prepare, requestID: requestID))
            guard isCurrent(token) else { return }
            recovery.setupAccepted(providerID: providerID, requestID: requestID, jobID: response.jobID)
            accepted = true
            try await refreshSnapshot(token: token)
            notice = "準備ジョブを受け付けました。準備完了と認証・生成の成功は別に確認します。"
            finish(token)
        } catch {
            guard isCurrent(token) else { return }
            if !accepted && publicFailure(error).code.rejectsBeforeAcceptance {
                recovery.setupRejected(providerID: providerID, requestID: requestID)
                fail(error, token: token)
                return
            }
            if !accepted { recovery.setupUnconfirmed(providerID: providerID) }
            fail(error, token: token)
            // This lookup can recover an accepted job; it never resubmits prepare.
            await refreshOperations()
        }
    }

    public func cancelSetup(providerID: ProviderID) async {
        guard canEdit, let pending = recovery.setups[providerID], pending.unresolved,
            let jobID = pending.jobID, let token = begin(.cancelling) else { return }
        do {
            let job = try await client.cancelJob(jobID: jobID)
            guard isCurrent(token) else { return }
            guard recovery.receive(job, providerID: providerID) else { throw ProviderSettingsFailure(.protocolError) }
            setupJobs[providerID] = job
            try await refreshSnapshot(token: token)
            finish(token)
        } catch {
            guard isCurrent(token) else { return }
            recovery.setupUnconfirmed(providerID: providerID)
            fail(error, token: token)
        }
    }

    /// Only local status tools are polled. No provider refresh, login or setup starts here.
    public func refreshOperations() async {
        guard canEdit, let token = begin(.checkingSetup) else { return }
        do {
            try await refreshSnapshot(token: token)
            for pending in recovery.authentications.values.filter(\.unresolved) {
                guard isCurrent(token) else { return }
                do { try await readAuthentication(pending, token: token) }
                catch {
                    guard isCurrent(token) else { return }
                    recovery.authenticationUnconfirmed(connectionID: pending.connectionID)
                    errorMessage = publicFailure(error).message
                }
            }
            for pending in recovery.setups.values.filter(\.unresolved) {
                guard isCurrent(token) else { return }
                guard let jobID = pending.jobID else { continue }
                do {
                    let job = try await client.jobStatus(jobID: jobID)
                    guard isCurrent(token) else { return }
                    guard recovery.receive(job, providerID: pending.providerID) else {
                        throw ProviderSettingsFailure(.protocolError)
                    }
                    setupJobs[pending.providerID] = job
                } catch {
                    guard isCurrent(token) else { return }
                    recovery.setupUnconfirmed(providerID: pending.providerID)
                    errorMessage = publicFailure(error).message
                }
            }
            try await refreshSnapshot(token: token)
            finish(token)
        } catch { fail(error, token: token) }
    }

    private func performAuthentication(_ action: ProviderAuthAction) async {
        guard let connection = selectedConnection, let token = begin(.authenticating) else { return }
        let requestID = UUID().uuidString
        guard recovery.beginAuthentication(connectionID: connection.connectionID, requestID: requestID) else {
            finish(token)
            return
        }
        editor.clearSensitiveInput()
        var accepted = false
        do {
            let response = try await client.authenticateProviderConnection(AuthenticateProviderConnectionRequest(
                connectionID: connection.connectionID, expectedRevision: connection.revision,
                action: action, requestID: requestID))
            guard isCurrent(token) else { return }
            guard response.operation.action == action, recovery.receive(response.operation) else {
                throw ProviderSettingsFailure(.protocolError)
            }
            accepted = true
            try await refreshSnapshot(token: token)
            notice = action == .logout && response.operation.state == .succeeded
                ? "この接続専用の認証情報を削除しました。ほかの接続には影響しません。"
                : "認証操作の状態を取得しました。議事録生成は実行していません。"
            finish(token)
        } catch {
            guard isCurrent(token) else { return }
            if accepted {
                fail(error, token: token)
                return
            }
            if publicFailure(error).code.rejectsBeforeAcceptance {
                recovery.authenticationRejected(connectionID: connection.connectionID, requestID: requestID)
                fail(error, token: token)
                return
            }
            recovery.authenticationUnconfirmed(connectionID: connection.connectionID)
            fail(error, token: token)
            await checkAuthentication()
        }
    }

    private func readAuthentication(
        _ pending: ProviderSettingsRecovery.Authentication, token: UInt64
    ) async throws {
        let response = try await client.providerAuthStatus(GetProviderAuthStatusRequest(
            connectionID: pending.connectionID, startRequestID: pending.startRequestID))
        guard isCurrent(token) else { return }
        guard recovery.receive(response.operation) else { throw ProviderSettingsFailure(.protocolError) }
    }

    private func publicFailure(_ error: Error) -> ProviderSettingsFailure {
        error as? ProviderSettingsFailure ?? ProviderSettingsFailure(.internalError)
    }
}
