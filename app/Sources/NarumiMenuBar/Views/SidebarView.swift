import NarumiMenuBarCore
import SwiftUI

/// 会議一覧 column: scope filter, free-text / transcript search, meeting rows with status and
/// active-job badges. All data comes from `list_meetings` / `search_transcripts` via the model.
struct SidebarView: View {
    @ObservedObject var model: MainWindowModel

    var body: some View {
        VStack(spacing: 0) {
            VStack(alignment: .leading, spacing: 6) {
                TextField("scope（空白区切り。空 = scope なしのみ）", text: $model.scopeText)
                    .textFieldStyle(.roundedBorder)
                    .onSubmit { Task { await model.refresh() } }
                TextField(
                    model.transcriptSearchEnabled ? "文字起こしを検索" : "会議名・engagement を検索",
                    text: $model.searchText
                )
                .textFieldStyle(.roundedBorder)
                .onSubmit { Task { await model.refresh() } }
                Toggle("文字起こしを検索（全文検索）", isOn: $model.transcriptSearchEnabled)
                    .toggleStyle(.checkbox)
                    .font(.caption)
                    .onChange(of: model.transcriptSearchEnabled) {
                        Task { await model.refresh() }
                    }
            }
            .padding(8)

            if let error = model.lastRefreshError {
                Label(error, systemImage: "exclamationmark.triangle")
                    .font(.caption)
                    .foregroundStyle(.orange)
                    .lineLimit(2)
                    .padding(.horizontal, 8)
                    .padding(.bottom, 4)
            }

            Divider()

            if model.showsSearchHits {
                searchHitList
            } else {
                meetingList
            }
        }
    }

    private var meetingList: some View {
        List(selection: $model.selectedMeetingID) {
            ForEach(model.meetings) { meeting in
                MeetingRowView(meeting: meeting)
                    .tag(meeting.meetingID)
            }
        }
        .listStyle(.sidebar)
        .overlay {
            if model.meetings.isEmpty {
                Text("会議がありません")
                    .foregroundStyle(.secondary)
            }
        }
        .onChange(of: model.selectedMeetingID) {
            Task { await model.selectionChanged() }
        }
    }

    private var searchHitList: some View {
        List {
            ForEach(model.searchHits) { hit in
                Button {
                    Task { await model.openSearchHit(hit) }
                } label: {
                    VStack(alignment: .leading, spacing: 2) {
                        HStack {
                            Text(hit.meetingName)
                                .font(.subheadline.bold())
                            Spacer()
                            Text(NarumiFormat.timecode(hit.start))
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        }
                        if let speaker = hit.speaker, !speaker.isEmpty {
                            Text(speaker)
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        }
                        Text(hit.text)
                            .font(.caption)
                            .lineLimit(3)
                    }
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
            }
        }
        .overlay {
            if model.searchHits.isEmpty {
                Text("一致する発話がありません")
                    .foregroundStyle(.secondary)
            }
        }
    }
}

struct MeetingRowView: View {
    let meeting: MeetingSummary

    var body: some View {
        let presentation = MeetingRowPresentation(meeting: meeting)
        VStack(alignment: .leading, spacing: 2) {
            Text(presentation.title)
                .lineLimit(1)
            Text(presentation.subtitle)
                .font(.caption)
                .foregroundStyle(.secondary)
                .lineLimit(1)
            if let jobText = presentation.jobText {
                Label(jobText, systemImage: "gearshape.2")
                    .font(.caption2)
                    .foregroundStyle(.blue)
                    .lineLimit(1)
            }
        }
        .padding(.vertical, 2)
    }
}
