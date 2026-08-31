import NarumiMenuBarCore
import SwiftUI

/// Acceptance recovery replays one immutable request only after a new explicit confirmation.
struct TranscriptionRequestRecoveryView: View {
    @ObservedObject var model: MainWindowModel
    @State private var confirmation: TranscriptionRequestRecoveryConfirmation?
    @State private var recovering = false
    @State private var recoveryTask: Task<Void, Never>?

    private var requests: [TranscriptionRequestRecovery] {
        model.transcriptionRequestRecovery.requests.filter { $0.meetingID == model.selectedMeetingID }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            if !requests.isEmpty {
                Label("音声再送の受付結果が未確認です", systemImage: "exclamationmark.arrow.triangle.2.circlepath")
                    .font(.headline).foregroundStyle(.orange)
                Text("新しい要求は作らず、元の要求を1回だけ再送して受付を確認できます。自動では再送しません。")
                    .font(.caption)
                ForEach(requests) { request in
                    VStack(alignment: .leading, spacing: 3) {
                        Text("元の要求: \(request.requestID)").font(.caption2).textSelection(.enabled)
                        Button("同じ要求を再送して受付を確認…") {
                            confirmation = model.transcriptionRequestRecovery.prepare(request)
                        }
                        .disabled(recovering || !canRecover)
                    }
                }
            }
            if let feedback = model.transcriptionRequestRecovery.feedback {
                Text(feedback).font(.caption).foregroundStyle(.secondary)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(requests.isEmpty && model.transcriptionRequestRecovery.feedback == nil ? 0 : 12)
        .background(.orange.opacity(0.08))
        .task(id: model.selectedMeetingID) { await model.transcriptionRequestRecovery.reload() }
        .popover(item: $confirmation, arrowEdge: .bottom) { confirmed in
            confirmationContent(confirmed)
        }
        .onChange(of: confirmation) {
            if confirmation == nil {
                recoveryTask?.cancel()
                model.transcriptionRequestRecovery.cancel()
            }
        }
        .onDisappear {
            recoveryTask?.cancel()
            model.transcriptionRequestRecovery.cancel()
            confirmation = nil
        }
    }

    private var canRecover: Bool {
        model.desktopSession.serverReachable && model.supportsTranscriptionModels
            && model.transcriptionModelCatalog.supportedProviders.contains("openai-api")
    }

    private func confirmationContent(_ confirmed: TranscriptionRequestRecoveryConfirmation) -> some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 10) {
                Text("元の要求を1回再送して受付を確認しますか？").font(.headline)
                LabeledContent("会議", value: confirmed.request.meetingID)
                LabeledContent("元の要求ID", value: confirmed.request.requestID)
                LabeledContent("元の接続ID", value: confirmed.request.expectedConfig.transcriptionModel?.connectionID ?? "未確認")
                LabeledContent("元の音声モデル", value: confirmed.request.expectedConfig.transcriptionModel?.modelID ?? "未確認")
                Text("元の要求が未受付だった場合は、この操作で音声処理・API 課金が始まる可能性があります。読み取りだけの状態確認ではありません。")
                    .font(.caption).foregroundStyle(.orange)
                Text("同じ要求ID・同じ保存設定・同じ区間の確認情報を使います。新しい試行番号・要求ID・再送対象は作りません。受付済みなら元のジョブを追跡し、結果が不明なままなら保留を維持します。")
                    .font(.caption)
                if model.detail?.meeting.meetingID != confirmed.request.meetingID {
                    Text("現在の会議設定は未確認です。下記の元の要求を別の設定へ置き換えることはありません。")
                        .font(.caption).foregroundStyle(.orange)
                } else if model.detail?.config != confirmed.request.expectedConfig {
                    Text("現在の保存設定と元の要求の設定は異なります。元の要求をそのまま確認し、現在の設定へ切り替えて送り直すことはありません。")
                        .font(.caption).foregroundStyle(.orange)
                }
                TranscriptionGenerationDisclosure(
                    config: confirmed.request.expectedConfig, catalog: model.transcriptionModelCatalog)
                Divider()
                MinutesGenerationDisclosure(config: confirmed.request.expectedConfig, catalog: model.minutesModelCatalog)
                Text("受付の成功応答が得られなければ、元の処理が未実行とは判断できません。取消や接続変更後に、自動でこの要求を送り直すことはありません。")
                    .font(.caption).foregroundStyle(.secondary)
                if let feedback = model.transcriptionRequestRecovery.feedback {
                    Text(feedback).font(.caption).foregroundStyle(.secondary)
                }
                HStack {
                    Button("キャンセル") {
                        recoveryTask?.cancel()
                        model.transcriptionRequestRecovery.cancel()
                        confirmation = nil
                    }
                    .keyboardShortcut(.cancelAction)
                    Spacer()
                    Button(recovering ? "受付を確認中…" : "同じ要求を1回再送") {
                        recovering = true
                        recoveryTask = Task {
                            _ = await model.confirmTranscriptionRequestRecovery(confirmed)
                            recovering = false
                            confirmation = nil
                            recoveryTask = nil
                        }
                    }
                    .disabled(recovering || !canRecover || !model.transcriptionRequestRecovery.canConfirm
                        || model.transcriptionRequestRecovery.confirmation?.id != confirmed.id
                        || model.selectedMeetingID != confirmed.request.meetingID)
                }
            }
            .padding(14)
        }
        .frame(width: 540)
        .frame(maxHeight: 680)
    }
}
