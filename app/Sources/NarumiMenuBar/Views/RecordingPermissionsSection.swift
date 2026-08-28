import NarumiMenuBarCore
import SwiftUI

struct RecordingPermissionsSection: View {
    @ObservedObject var model: MainWindowModel

    private var state: RecordingPermissionSetupState { model.permissionSetup }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Label("録画の権限", systemImage: "lock.shield")
                .font(.headline)
            Text("ここでは録画を開始せず、macOS の許可だけを確認・設定します。許可後の録画開始は別の操作です。")
                .font(.callout)
                .foregroundStyle(.secondary)
            if state.serverReachable && !state.supportsSetup {
                Text("この接続では権限設定の安全性を確認できません。起動個体の識別に対応した新しいサーバーに更新してください。")
                    .font(.callout)
                    .foregroundStyle(.orange)
            }
            HStack(alignment: .top, spacing: 10) {
                permissionCard(.microphone)
                permissionCard(.screenRecording)
            }
            if state.blocked {
                HStack(alignment: .top, spacing: 8) {
                    ProgressView().controlSize(.small)
                    Text(state.isActionInFlight
                        ? "macOS の許可操作を待っています。ダイアログを確認してください（最大約2分）。"
                        : "許可操作の完了を確認しています。確認できるまで録画開始・再起動・終了・更新を待機します。自動再送はしません。")
                        .font(.callout)
                }
                .foregroundStyle(.orange)
            }
            if let feedback = model.permissionFeedback {
                Text(feedback)
                    .font(.callout)
                    .textSelection(.enabled)
            }
            HStack {
                Button(state.serverReachable ? "状態を再確認" : "接続を再確認") {
                    Task { await model.refreshRecordingPermissions() }
                }
                .disabled(model.refreshingPermissions)
                if model.refreshingPermissions {
                    ProgressView().controlSize(.small)
                }
                Spacer()
            }
            Text("設定画面が開かない場合は、各カードの手順で移動してください。macOS から再起動を求められた場合は、その案内に従ってください。")
                .font(.caption)
                .foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private func permissionCard(_ permission: RecordingPermission) -> some View {
        GroupBox {
            VStack(alignment: .leading, spacing: 8) {
                permissionStatus(permission)
                Text(permission.purpose)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                if state.permissionState(permission) != .granted {
                    if permission == .screenRecording || state.permissionState(permission) != .notGranted {
                        Button("許可を求める") {
                            model.configureRecordingPermission(permission, action: .request)
                        }
                        .disabled(!state.canRequest(permission))
                    }
                    Button("macOS の設定を開く") {
                        model.configureRecordingPermission(permission, action: .openSettings)
                    }
                    .disabled(!state.canOpenSettings(permission))
                }
                Text(permission.settingsPath)
                    .font(.caption)
                    .textSelection(.enabled)
                    .fixedSize(horizontal: false, vertical: true)
                if permission == .screenRecording {
                    Text("項目名は macOS の版で異なります。未許可には、まだ要求していない場合も含まれます。")
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                } else if state.permissionState(permission) == .notGranted {
                    Text("拒否済みの場合は、システム設定でマイクを許可してください。")
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                }
            }
            .controlSize(.small)
            .frame(maxWidth: .infinity, alignment: .leading)
        } label: {
            Label(permission.displayName, systemImage: permission.systemSymbol)
        }
        .frame(maxWidth: .infinity, alignment: .topLeading)
    }

    @ViewBuilder
    private func permissionStatus(_ permission: RecordingPermission) -> some View {
        switch state.permissionState(permission) {
        case .granted:
            Label("許可済み", systemImage: "checkmark.circle.fill").foregroundStyle(.green)
        case .notGranted:
            Label("未許可", systemImage: "exclamationmark.circle").foregroundStyle(.orange)
        case .unknown:
            Label("未確認", systemImage: "questionmark.circle").foregroundStyle(.secondary)
        case .helperUnavailable:
            Label("ヘルパーが見つかりません", systemImage: "exclamationmark.triangle").foregroundStyle(.orange)
        case .unreachable:
            Label("確認できません", systemImage: "wifi.exclamationmark").foregroundStyle(.orange)
        }
    }
}
