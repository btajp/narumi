import NarumiMenuBarCore
import SwiftUI

/// Detail column: meeting header + 議事録 / 文字起こし / コンテキスト / 設定 tabs.
struct MeetingDetailView: View {
    @ObservedObject var model: MainWindowModel

    var body: some View {
        VStack(spacing: 0) {
            header
            Divider()
            playback
            Divider()
            Picker("タブ", selection: $model.selectedTab) {
                ForEach(MainWindowModel.DetailTab.allCases) { tab in
                    Text(tab.rawValue).tag(tab)
                }
            }
            .pickerStyle(.segmented)
            .labelsHidden()
            .padding(8)
            .onChange(of: model.selectedTab) {
                Task { await model.tabChanged() }
            }
            Divider()
            switch model.selectedTab {
            case .minutes:
                MinutesTabView(model: model)
            case .transcript:
                TranscriptTabView(model: model)
            case .contexts:
                ContextsTabView(model: model)
            case .settings:
                SettingsTabView(model: model)
            }
        }
    }

    private var header: some View {
        HStack(alignment: .firstTextBaseline) {
            VStack(alignment: .leading, spacing: 2) {
                Text(model.detail?.meeting.meetingName ?? model.selectedMeetingID ?? "")
                    .font(.title3.bold())
                    .lineLimit(1)
                if let meeting = model.detail?.meeting {
                    Text(MeetingRowPresentation(meeting: meeting).subtitle)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
            Spacer()
            if model.detail != nil {
                Button {
                    model.openBundleInFinder()
                } label: {
                    Label("バンドルを Finder で開く", systemImage: "folder")
                }
            }
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
    }

    @ViewBuilder
    private var playback: some View {
        if let presentation = model.playbackPresentation {
            VStack(alignment: .leading, spacing: 6) {
                if let warning = presentation.recorderWarning {
                    Label(warning, systemImage: "exclamationmark.triangle")
                        .foregroundStyle(.orange)
                    if let error = model.detail?.recording.recorderError {
                        Text(error.message).font(.caption).foregroundStyle(.secondary)
                    }
                }
                HStack(alignment: .center, spacing: 12) {
                    VStack(alignment: .leading, spacing: 3) {
                        Text("統合録画").font(.subheadline.bold())
                        Text(presentation.message).font(.caption).foregroundStyle(.secondary)
                        if let display = model.detail?.recording.display {
                            Text("録画画面: \(display.name) (\(display.width) × \(display.height))")
                                .font(.caption).foregroundStyle(.secondary)
                        }
                    }
                    Spacer()
                    if presentation.playbackPath != nil {
                        Button {
                            Task { await model.playRecording() }
                        } label: {
                            Label("再生", systemImage: "play.fill")
                        }
                    }
                    if presentation.canPrepare {
                        Button(presentation.preparationLabel) {
                            Task { await model.preparePlayback() }
                        }
                        .disabled(model.playbackBusy || !model.desktopSession.serverReachable)
                    }
                }
                if model.playbackBusy {
                    HStack(spacing: 6) {
                        ProgressView().controlSize(.small)
                        Text(model.jobs.first { $0.meetingID == model.detail?.meeting.meetingID && $0.isActive }.map {
                            NarumiFormat.jobText(kind: $0.kind, status: $0.status, progress: $0.progress)
                        } ?? "処理の状態を確認中…")
                            .font(.caption)
                    }
                } else if let job = model.playbackJob, presentation.playbackPath == nil,
                    job.status == "failed" || job.status == "cancelled" {
                    Text(job.error?.message ?? "録画ファイルの生成を完了できませんでした。")
                        .font(.caption).foregroundStyle(.red)
                }
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 8)
        }
    }
}
