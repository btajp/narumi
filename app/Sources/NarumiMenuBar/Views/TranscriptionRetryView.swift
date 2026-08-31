import NarumiMenuBarCore
import SwiftUI

/// Unknown audio chunks require a separate, one-use confirmation, never an epoch-only retry.
struct TranscriptionRetryView: View {
    @ObservedObject var model: MainWindowModel
    @State private var confirmation: TranscriptionRetryConfirmation?
    @State private var confirming = false
    @State private var confirmationTask: Task<Void, Never>?

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            if let job = model.transcriptionUnknownJob, let details = job.error?.transcriptionOutcome {
                Label("音声認識の送信結果が不明です", systemImage: "exclamationmark.triangle")
                    .font(.headline).foregroundStyle(.orange)
                Text(chunkDescription(details))
                Text("自動再送は停止しています。API 側の処理が完了している可能性があり、再送すると利用料が重複する場合があります。")
                    .font(.caption)
                Button("不明区間を再送…") {
                    confirmation = model.prepareTranscriptionRetry(job: job)
                }
                .disabled(confirming || !model.supportsTranscriptionModels
                    || !model.transcriptionModelCatalog.supportedProviders.contains("openai-api"))
            }
            if let feedback = model.transcriptionRetry.feedback {
                Text(feedback).font(.caption).foregroundStyle(.secondary)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(model.transcriptionUnknownJob == nil && model.transcriptionRetry.feedback == nil ? 0 : 12)
        .background(.orange.opacity(0.08))
        .popover(item: $confirmation, arrowEdge: .bottom) { confirmed in
            confirmationContent(confirmed)
        }
        .onChange(of: confirmation) {
            if confirmation == nil {
                confirmationTask?.cancel()
                model.transcriptionRetry.cancel()
            }
        }
        .onDisappear {
            confirmationTask?.cancel()
            model.transcriptionRetry.cancel()
            confirmation = nil
        }
    }

    private func confirmationContent(_ confirmed: TranscriptionRetryConfirmation) -> some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 10) {
                Text("この不明区間を再送しますか？").font(.headline)
                Text(chunkDescription(confirmed.details)).bold()
                LabeledContent("完了済み区間数", value: String(confirmed.details.completedChunks))
                LabeledContent("確認元のジョブ", value: confirmed.jobID)
                Text("成功済みの区間は再利用します。ほかの結果不明区間には別の確認が必要です。未送信の区間は処理を続けて送信する場合があります。")
                    .font(.caption)
                Text("元の送信結果を回収する操作ではありません。同じ音声を OpenAI API に再度送信するため、API 利用料が重複する可能性があります。取消してもサービス側の処理や課金の停止は保証できません。")
                    .font(.caption).foregroundStyle(.orange)
                TranscriptionGenerationDisclosure(config: confirmed.config, catalog: model.transcriptionModelCatalog)
                Divider()
                MinutesGenerationDisclosure(config: confirmed.config, catalog: model.minutesModelCatalog)
                Text("音声認識の試行番号を \(confirmed.config.transcriptionModel?.cacheEpoch ?? 0) から \(confirmed.updatedConfig.transcriptionModel?.cacheEpoch ?? 0) に保存し、保存内容が確認した設定と完全一致した場合だけ再送要求を送ります。試行番号の変更だけでは再送しません。")
                    .font(.caption).foregroundStyle(.secondary)
                if let message = model.generationValidationMessage(config: confirmed.config) {
                    Text(message).font(.caption).foregroundStyle(.orange)
                }
                if let feedback = model.transcriptionRetry.feedback {
                    Text(feedback).font(.caption).foregroundStyle(.secondary)
                }
                HStack {
                    Button("キャンセル") {
                        confirmationTask?.cancel()
                        model.transcriptionRetry.cancel()
                        confirmation = nil
                    }
                    .keyboardShortcut(.cancelAction)
                    Spacer()
                    Button(confirming ? "確認・送信中…" : "API への再送を開始") {
                        confirming = true
                        confirmationTask = Task {
                            _ = await model.confirmTranscriptionRetry(confirmed)
                            confirming = false
                            confirmation = nil
                            confirmationTask = nil
                        }
                    }
                    .disabled(confirming || !model.transcriptionRetry.canConfirm
                        || model.transcriptionRetry.confirmation?.id != confirmed.id
                        || model.selectedMeetingID != confirmed.meetingID
                        || model.detail?.config != confirmed.config
                        || model.generationValidationMessage(config: confirmed.config) != nil)
                }
            }
            .padding(14)
        }
        .frame(width: 540)
        .frame(maxHeight: 680)
    }

    private func chunkDescription(_ details: TranscriptionOutcomeUnknownDetails) -> String {
        let track = details.track == .mic ? "マイク" : "システム音声"
        return "\(track) · 区間 \(details.chunkIndex + 1) / \(details.chunkCount) · "
            + "\(NarumiFormat.timecode(details.startSeconds)) – \(NarumiFormat.timecode(details.endSeconds))"
    }
}
