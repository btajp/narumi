import NarumiMenuBarCore
import SwiftUI

/// 文字起こし tab: source picker (`get_transcript` merged / own-mic / own-system / ext-*),
/// segment table with resolved speaker names.
struct TranscriptTabView: View {
    @ObservedObject var model: MainWindowModel

    var body: some View {
        VStack(spacing: 0) {
            controls
            Divider()
            content
        }
    }

    private var controls: some View {
        HStack {
            if let transcript = model.transcript, !transcript.availableSources.isEmpty {
                Picker("ソース", selection: Binding(
                    get: { model.selectedTranscriptSource ?? transcript.source },
                    set: { source in Task { await model.transcriptSourceChanged(source) } }
                )) {
                    ForEach(transcript.availableSources, id: \.self) { source in
                        Text(source).tag(source)
                    }
                }
                .fixedSize()
            }
            Spacer()
            if let transcript = model.transcript {
                Text("\(transcript.segments.count) セグメント")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
        .padding(8)
    }

    @ViewBuilder
    private var content: some View {
        if let transcript = model.transcript {
            Table(transcript.segments) {
                TableColumn("開始") { segment in
                    Text(NarumiFormat.timecode(segment.start))
                        .monospacedDigit()
                }
                .width(min: 50, ideal: 60, max: 90)
                TableColumn("話者") { segment in
                    Text(Self.speakerLabel(segment: segment, speakerMap: transcript.speakerMap))
                        .lineLimit(1)
                }
                .width(min: 70, ideal: 110, max: 180)
                TableColumn("発話") { segment in
                    Text(segment.text)
                        .lineLimit(nil)
                        .textSelection(.enabled)
                }
            }
        } else if let text = model.transcriptUnavailable {
            ContentUnavailableView(
                "文字起こしがありません", systemImage: "waveform",
                description: Text(text))
        } else {
            ProgressView("読み込み中…")
                .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
    }

    /// Resolved name → raw speaker label → empty.
    static func speakerLabel(segment: TranscriptSegment, speakerMap: [String: SpeakerIdentity]) -> String {
        if let name = segment.speakerName, !name.isEmpty {
            return name
        }
        guard let speaker = segment.speaker else {
            return ""
        }
        if let mapped = speakerMap[speaker]?.name, !mapped.isEmpty {
            return mapped
        }
        return speaker
    }
}
