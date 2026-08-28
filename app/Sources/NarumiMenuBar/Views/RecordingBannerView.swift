import NarumiMenuBarCore
import SwiftUI

/// Always-visible recording entry point; both controls use the menu bar's state and actions.
struct RecordingBannerView: View {
    @ObservedObject var model: MainWindowModel

    private var session: DesktopSessionState { model.desktopSession }

    var body: some View {
        HStack(spacing: 14) {
            Image(systemName: session.menuSymbolName)
                .font(.title2)
                .foregroundStyle(session.recording.active ? .red : .secondary)
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 4) {
                Text(session.statusText)
                    .font(.headline)
                if session.recording.active {
                    Text(session.recording.meetingName ?? session.recording.meetingID ?? "会議")
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                } else {
                    Text(model.permissionSetup.needsSetup
                        ? "マイクと画面収録の許可が必要です。診断画面で設定を確認できます。"
                        : "会議名を入力して録画を開始します。終了後はここから停止できます。")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
            Spacer(minLength: 12)
            if session.recording.active, let elapsed = session.recording.elapsedSec {
                Text(NarumiFormat.duration(elapsed))
                    .font(.title3.monospacedDigit())
                    .accessibilityLabel("録画経過時間 \(NarumiFormat.duration(elapsed))")
            }
            if session.operation != nil || model.permissionSetup.blocked {
                ProgressView()
                    .controlSize(.small)
            }
            if session.recording.active {
                Button(action: model.stopRecordingFromBanner) {
                    Label("録画停止", systemImage: "stop.circle.fill")
                }
                .disabled(!session.canStop)
                .help("録画を停止して保存します")
            } else if model.permissionSetup.needsSetup || model.permissionSetup.blocked {
                Button(action: model.openPermissionSetup) {
                    Label(model.permissionSetupButtonTitle, systemImage: "lock.shield")
                }
                .disabled(!model.canOpenPermissionSetup)
                .tint(.accentColor)
                .help("録画せずに、マイクと画面収録の許可を設定します")
            } else {
                Button(action: model.startRecordingFromWindow) {
                    Label("録画開始", systemImage: "record.circle")
                }
                .disabled(!session.canStart)
                .help(session.canStart ? "会議名を入力して録画を開始します" : session.statusText)
            }
        }
        .labelStyle(.titleAndIcon)
        .buttonStyle(.borderedProminent)
        .tint(.red)
        .controlSize(.large)
        .padding(.horizontal, 20)
        .padding(.vertical, 14)
        .background(session.recording.active ? Color.red.opacity(0.08) : Color.primary.opacity(0.03))
        Divider()
    }
}
