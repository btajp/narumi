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
                    Label("会議フォルダを Finder で表示", systemImage: "folder")
                }
                .help("元の録画・音声データと議事録を含む会議フォルダを表示します。統合 MP4 は下の「MP4 を Finder で表示」から開けます。")
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
                Text("録画ファイル（MP4）").font(.subheadline.bold())
                Text(presentation.message).font(.caption).foregroundStyle(.secondary)
                if let display = model.detail?.recording.display {
                    Text("録画画面: \(display.name) (\(display.width) × \(display.height))")
                        .font(.caption).foregroundStyle(.secondary)
                }
                ViewThatFits(in: .horizontal) {
                    HStack(spacing: 12) { playbackActions(presentation) }
                    VStack(alignment: .leading, spacing: 8) { playbackActions(presentation) }
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
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.horizontal, 12)
            .padding(.vertical, 8)
        }
    }

    @ViewBuilder
    private func playbackActions(_ presentation: RecordingPlaybackPresentation) -> some View {
        if presentation.playbackPath != nil {
            Button {
                Task { await model.revealRecordingInFinder() }
            } label: {
                Label("MP4 を Finder で表示", systemImage: "doc")
            }
            .buttonStyle(.borderedProminent)
            Button {
                Task { await model.playRecording() }
            } label: {
                Label("録画を再生", systemImage: "play.fill")
            }
            .buttonStyle(.bordered)
        }
        if presentation.canPrepare {
            if presentation.playbackPath == nil {
                Button(presentation.preparationLabel) {
                    Task { await model.preparePlayback() }
                }
                .buttonStyle(.borderedProminent)
                .disabled(model.playbackBusy || !model.desktopSession.serverReachable)
            } else {
                Button(presentation.preparationLabel) {
                    Task { await model.preparePlayback() }
                }
                .buttonStyle(.borderless)
                .disabled(model.playbackBusy || !model.desktopSession.serverReachable)
            }
        }
    }
}
