import NarumiMenuBarCore
import SwiftUI

/// Detail column: meeting header + 議事録 / 文字起こし / コンテキスト / 設定 tabs.
struct MeetingDetailView: View {
    @ObservedObject var model: MainWindowModel

    var body: some View {
        VStack(spacing: 0) {
            header
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
}
