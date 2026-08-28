import AppKit
import Foundation
import NarumiMenuBarCore

extension AppDelegate {
    struct PermissionOwnedContext {
        let token: ServerLauncher.OwnedServerToken
        let connectionGeneration: UInt64
    }

    var canRouteToPermissionSetup: Bool {
        permissionSetup.needsSetup && !permissionSetup.blocked && !session.recording.active
            && session.operation == nil && !session.terminating && !session.installingUpdate
    }

    func synchronizePermissionUI() {
        permissionSetup.setRecordingBusy(
            session.recording.active || session.operation != nil || session.terminating
                || session.installingUpdate || launcher?.isBusy != false
                || !session.serverReachable || !session.recordingIsConfirmed)
        session.setPermissionSetupState(blocked: permissionSetup.blocked, needsSetup: permissionSetup.needsSetup)
        mainWindowModel?.permissionSetup = permissionSetup
    }

    func openPermissionDiagnostics() {
        openMainWindow()
        ensureMainWindowModel().showDiagnosticsSheet = true
    }

    func configureRecordingPermission(_ permission: RecordingPermission, action: RecordingPermissionAction) {
        synchronizePermissionUI()
        guard let sessionGeneration = permissionSessionGeneration else {
            Task { await refreshRecordingPermissions() }
            return
        }
        guard let token = permissionSetup.beginAction(permission: permission, action: action) else { return }
        capturePermissionOwnerIfNeeded()
        let requestID = UUID().uuidString
        let contractVersion = permissionSetup.serverInfo?.contractVersion
        let serverInstanceID = permissionSetup.serverInfo?.serverInstanceID
        ensureMainWindowModel().permissionFeedback = nil
        applyServerState()
        Task {
            guard permissionSetup.isCurrentAction(token) else { return }
            do {
                let response = try await NarumiClient(mcp: client).configureRecordingPermission(
                    permission, action: action, requestID: requestID, contractVersion: contractVersion,
                    serverInstanceID: serverInstanceID, sessionGeneration: sessionGeneration)
                if permissionSetup.finishAction(token, response: response) {
                    if response.settingsOpened {
                        mainWindowModel?.permissionFeedback =
                            "macOS の設定を開きました。許可を変更して narumi に戻ると状態を再確認します。"
                    } else if permissionSetup.ready {
                        mainWindowModel?.permissionFeedback = "許可を確認しました。録画は「録画開始」から操作してください。"
                    } else {
                        mainWindowModel?.permissionFeedback =
                            "許可操作が終了しました。未許可の項目はカードの手順で設定してください。"
                    }
                }
            } catch {
                let reason = (error as? ToolFailure)?.message ?? error.localizedDescription
                if permissionSetup.failAction(token, message: reason) {
                    mainWindowModel?.permissionFeedback =
                        "権限操作の結果を確認できません。自動再送せず、サーバーの状態を再確認します。\n\(reason)"
                    permissionNeedsCapabilityProbe = true
                    permissionSessionGeneration = nil
                }
            }
            applyServerState()
            await refreshRecordingPermissions()
        }
    }

    func refreshRecordingPermissions() async {
        reconcileExitedPermissionOwner()
        await refreshServerStatus(refreshPermissions: true)
        if !permissionSetup.serverReachable {
            let model = ensureMainWindowModel()
            if permissionSetup.blocked {
                model.permissionFeedback = "サーバーに接続できず、許可操作の完了を確認できません。「接続を再確認」から確認してください。"
            } else if model.permissionFeedback == nil {
                model.permissionFeedback = "サーバーに接続できません。起動状態とログを確認してください。"
            }
        }
        applyServerState()
    }

    /// Always identify an unknown/reconnected server with the legacy empty input first.
    /// A requested fresh read then uses the version bound to that same connection.
    func readPermissionServerInfo(refreshPermissions: Bool) async throws -> ServerInfo {
        let generation = session.connectionGeneration
        let requestFresh = refreshPermissions && !permissionNeedsCapabilityProbe
            && permissionSetup.serverReachable && permissionSessionGeneration != nil
        guard let token = permissionSetup.beginSnapshot(refreshPermissions: requestFresh) else {
            throw ToolFailure(code: "busy", message: "権限の状態を確認しています。")
        }
        let knownVersion = permissionSetup.serverInfo?.contractVersion
        do {
            let arguments = RecordingPermissionContract.serverInfoArguments(
                contractVersion: knownVersion, serverInstanceID: permissionSetup.serverInfo?.serverInstanceID,
                refreshPermissions: token.refreshPermissions).mapValues(JSONNode.bool)
            let result = try await client.callTool(
                ToolCatalog.getServerInfo, arguments: arguments,
                expectedSessionGeneration: token.refreshPermissions ? permissionSessionGeneration : nil)
            guard let content = result.structuredContent else {
                throw ToolFailure(code: "protocol", message: "診断の応答を確認できません。")
            }
            let info = try JSONDecoder().decode(ServerInfo.self, from: content.serialized())
            guard generation == session.connectionGeneration else {
                throw ToolFailure(code: "stale", message: "接続が切り替わったため状態を再確認します。")
            }
            let wasBlocked = permissionSetup.blocked
            let accepted = permissionSetup.finishSnapshot(token, info: info)
            if accepted {
                permissionNeedsCapabilityProbe = false
                permissionSessionGeneration = result.sessionGeneration
                capturePermissionOwnerIfNeeded()
                mainWindowModel?.serverInfo = info
                if !permissionSetup.blocked && (wasBlocked || mainWindowModel?.refreshingPermissions == true) {
                    mainWindowModel?.permissionFeedback = permissionSetup.ready
                        ? "許可操作の完了を確認しました。録画は「録画開始」から操作してください。"
                        : "許可操作の完了を確認しました。未許可の項目を設定してください。"
                }
                applyServerState()
            }
            if accepted && refreshPermissions && !token.refreshPermissions
                && permissionSessionGeneration != nil && permissionSetup.supportsSetup {
                return try await readPermissionServerInfo(refreshPermissions: true)
            }
            return info
        } catch {
            if permissionSetup.failSnapshot(token) {
                permissionNeedsCapabilityProbe = true
                permissionSessionGeneration = nil
            }
            reconcileExitedPermissionOwner()
            synchronizePermissionUI()
            throw error
        }
    }

    func capturePermissionOwnerIfNeeded() {
        guard permissionSetup.blocked else {
            permissionOwnedContext = nil
            return
        }
        guard permissionOwnedContext == nil,
            let generation = permissionSetup.recoveryConnectionGeneration,
            let pid = permissionSetup.recoveryServerPID,
            let token = launcher.captureOwnedServerToken(), token.serverPID == pid
        else { return }
        permissionOwnedContext = PermissionOwnedContext(token: token, connectionGeneration: generation)
    }

    /// A stopped state or a missing direct PID alone cannot release this gate. The launcher
    /// must prove the original owned process group exited under the same runtime lease.
    func reconcileExitedPermissionOwner() {
        guard permissionSetup.blocked, let context = permissionOwnedContext,
            permissionSetup.recoveryConnectionGeneration == context.connectionGeneration,
            launcher?.confirmOwnedProcessTreeExited(context.token) == true
        else { return }
        let evidence = RecordingPermissionSetupState.OwnedProcessTerminationEvidence(
            connectionGeneration: context.connectionGeneration, serverPID: context.token.serverPID,
            serverExited: true, allOwnedChildrenExited: true)
        if permissionSetup.confirmOwnedProcessTermination(evidence) {
            permissionOwnedContext = nil
            mainWindowModel?.permissionFeedback =
                "所有サーバーと子プロセスの終了を確認しました。必要なら「サーバーを再起動」を選んでください。権限は再起動後に再確認します。"
            synchronizePermissionUI()
        }
    }
}
