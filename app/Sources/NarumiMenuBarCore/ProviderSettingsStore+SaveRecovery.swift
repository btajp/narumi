import Foundation

extension ProviderSettingsStore {
    public var saveRecoverySummary: ProviderSaveRecoverySummary? {
        guard let pendingSave else { return nil }
        return ProviderSaveRecoverySummary(
            requestID: pendingSave.requestID, connectionID: pendingSave.connectionID,
            providerID: pendingSave.providerID, displayName: pendingSave.displayName,
            endpoint: pendingSave.endpoint, enabled: pendingSave.enabled,
            requiresAPIKeyReentry: pendingSave.credentialIntent == .reenter,
            receiptConfirmed: pendingSave.receiptConnectionID != nil)
    }

    public var canRetryPendingSave: Bool {
        canEdit && pendingSave != nil && pendingSave?.receiptConnectionID == nil
    }

    public var canAdoptSavedConnectionForRecovery: Bool {
        guard canEdit, recoverySnapshotAvailable, let connection = selectedConnection else { return false }
        return pendingSave?.canAdoptAfterReview(connection) == true
    }

    public var canDiscardMissingConnectionChange: Bool {
        guard canEdit, recoverySnapshotAvailable, let connectionID = pendingSave?.connectionID else { return false }
        return !connections.contains { $0.connectionID == connectionID }
    }

    /// The key is supplied afresh by the SecureField, never copied into recovery state.
    /// The caller clears its input before invoking this explicit, same-request retry.
    public func retryPendingSave(apiKey: String? = nil) async {
        guard canRetryPendingSave, let pendingSave else { return }
        guard let request = pendingSave.retryRequest(apiKey: apiKey) else {
            errorMessage = "前回と同じ API キーを再入力してください。保存は送信していません。"
            return
        }
        guard let token = begin(.reconcilingSave) else { return }
        recoverySnapshotAvailable = false
        do {
            let response = try await client.setProviderConnection(request)
            guard isCurrent(token) else { return }
            guard self.pendingSave?.confirmReceipt(response.connection) == true else {
                throw ProviderSettingsFailure(.protocolError)
            }
            // A replay receipt can predate a rename, credential change or deletion.
            // It proves the target ID only; current settings come from the public list.
            try await refreshSnapshot(token: token)
            finish(token)
        } catch {
            // A retry conflict can mean a mistyped replacement key. It never proves
            // the original request was rejected, so keep its recovery state intact.
            fail(error, token: token, saving: true)
        }
    }

    /// After reviewing a fresh public snapshot, editing this existing ID is safe even
    /// when its name changed or the previous credential write failed.
    public func adoptSavedConnectionAfterReview(connectionID: String, expectedRevision: Int) {
        guard canAdoptSavedConnectionForRecovery, let connection = selectedConnection,
            connection.connectionID == connectionID, connection.revision == expectedRevision else {
            errorMessage = ProviderSettingsFailure(.configurationConflict).message
            return
        }
        restoreEditing(connection)
        notice = "確認した保存済み接続から編集を再開しました。次の保存は現在の版を基準に行います。"
    }

    /// A delayed update to an already deleted ID cannot create another connection.
    /// This exit is deliberately unavailable for an unresolved create with no known ID.
    public func discardMissingConnectionChangeAfterReview() {
        guard canDiscardMissingConnectionChange else { return }
        pendingSave = nil
        recoverySnapshotAvailable = false
        chooseInitialConnection()
        clearSelectionFeedback()
        notice = "一覧に存在しない接続への未確認の編集を破棄しました。保存要求は再送していません。"
    }

    func reconcileSavedSnapshot() {
        guard let pendingSave else { return }
        recoverySnapshotAvailable = true
        if let saved = pendingSave.confirmedConnection(in: connections) {
            restoreEditing(saved)
            notice = "保存済み接続の現在の設定を取得しました。内容を確認してください。"
        } else if pendingSave.receiptConnectionID != nil {
            // The original write completed, but its ID has since been removed.
            self.pendingSave = nil
            recoverySnapshotAvailable = false
            chooseInitialConnection()
            clearSelectionFeedback()
            notice = "元の保存要求の完了は確認できましたが、接続は現在の一覧に存在しません。"
        }
    }

    private func restoreEditing(_ connection: ProviderConnection) {
        pendingSave = nil
        recoverySnapshotAvailable = false
        selectedConnectionID = connection.connectionID
        editor.adopt(connection)
        clearSelectionFeedback()
    }
}
