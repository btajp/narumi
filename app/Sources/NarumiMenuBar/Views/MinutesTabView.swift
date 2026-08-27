import NarumiMenuBarCore
import SwiftUI

/// 議事録 tab: version picker (`get_minutes`), markdown preview, unresolved-speakers callout,
/// regenerate (`regenerate` + job tracking), export (`list_export_destinations` /
/// `export_minutes`), versions list and export history (`get_meeting`).
struct MinutesTabView: View {
    @ObservedObject var model: MainWindowModel
    @State private var regenerateForce = false
    @State private var regenerateReason = ""
    @State private var showRegeneratePopover = false

    var body: some View {
        VStack(spacing: 0) {
            controls
            Divider()
            content
        }
    }

    private var controls: some View {
        HStack(spacing: 12) {
            if let minutes = model.minutes, !minutes.availableVersions.isEmpty {
                Picker("版", selection: Binding(
                    get: { model.selectedMinutesVersion ?? minutes.version },
                    set: { version in Task { await model.minutesVersionChanged(version) } }
                )) {
                    ForEach(minutes.availableVersions, id: \.self) { version in
                        Text("v\(version)").tag(version)
                    }
                }
                .fixedSize()
            }

            Button {
                showRegeneratePopover = true
            } label: {
                Label("再生成", systemImage: "arrow.clockwise")
            }
            .popover(isPresented: $showRegeneratePopover, arrowEdge: .bottom) {
                regeneratePopover
            }

            Menu {
                if model.exportDestinations.isEmpty {
                    Text("エクスポート先がありません")
                } else {
                    ForEach(model.exportDestinations) { destination in
                        Button("\(destination.name) — \(destination.description)") {
                            Task { await model.export(destination: destination) }
                        }
                    }
                }
            } label: {
                Label("エクスポート", systemImage: "square.and.arrow.up")
            }
            .fixedSize()
            .disabled(model.minutes == nil)

            Spacer()

            if let minutes = model.minutes {
                Text("\(NarumiFormat.jstDateTime(minutes.generatedAt)) · \(minutes.provider)")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
        .padding(8)
    }

    private var regeneratePopover: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("議事録を再生成").font(.headline)
            Toggle("強制再生成（入力が同じでも新しい版を作る）", isOn: $regenerateForce)
                .toggleStyle(.checkbox)
            TextField("理由（任意。manifest に記録されます）", text: $regenerateReason)
                .textFieldStyle(.roundedBorder)
                .frame(width: 320)
            HStack {
                Spacer()
                Button("再生成を開始") {
                    showRegeneratePopover = false
                    Task {
                        await model.regenerate(force: regenerateForce, reason: regenerateReason)
                        regenerateReason = ""
                        regenerateForce = false
                    }
                }
                .keyboardShortcut(.defaultAction)
            }
        }
        .padding(12)
    }

    @ViewBuilder
    private var content: some View {
        if let minutes = model.minutes {
            ScrollView {
                VStack(alignment: .leading, spacing: 12) {
                    if !minutes.unresolvedSpeakers.isEmpty {
                        unresolvedCallout(minutes.unresolvedSpeakers)
                    }
                    MarkdownBlocksView(markdown: minutes.markdown)
                    historySections
                }
                .padding(12)
            }
        } else if let text = model.minutesUnavailable {
            ContentUnavailableView(
                "議事録がありません", systemImage: "doc.text",
                description: Text(text))
        } else {
            ProgressView("読み込み中…")
                .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
    }

    private func unresolvedCallout(_ speakers: [String]) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Image(systemName: "person.fill.questionmark")
                .foregroundStyle(.orange)
            VStack(alignment: .leading, spacing: 2) {
                Text("実名未解決の話者: \(speakers.joined(separator: ", "))")
                    .bold()
                Text("話者名付きの外部トランスクリプト（Notion AI 議事録など）をコンテキストに登録して再生成すると解決できます。")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
        .padding(10)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(.orange.opacity(0.12), in: RoundedRectangle(cornerRadius: 8))
    }

    @ViewBuilder
    private var historySections: some View {
        if let detail = model.detail {
            if !detail.minutesVersions.isEmpty {
                GroupBox("版の履歴") {
                    VStack(alignment: .leading, spacing: 4) {
                        ForEach(detail.minutesVersions) { info in
                            HStack {
                                Text("v\(info.version)")
                                    .monospacedDigit()
                                    .frame(width: 36, alignment: .leading)
                                Text(NarumiFormat.jstDateTime(info.generatedAt))
                                Text(info.provider)
                                    .foregroundStyle(.secondary)
                                Spacer()
                                Button("表示") {
                                    Task { await model.minutesVersionChanged(info.version) }
                                }
                                .buttonStyle(.link)
                            }
                            .font(.caption)
                        }
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                }
            }
            if !detail.exports.isEmpty {
                GroupBox("エクスポート履歴") {
                    VStack(alignment: .leading, spacing: 4) {
                        ForEach(Array(detail.exports.enumerated()), id: \.offset) { _, record in
                            HStack {
                                Text(record.destination)
                                    .frame(width: 80, alignment: .leading)
                                Text("v\(record.minutesVersion)")
                                    .monospacedDigit()
                                Text(NarumiFormat.jstDateTime(record.at))
                                    .foregroundStyle(.secondary)
                                Spacer()
                                Button {
                                    model.revealRef(record.ref)
                                } label: {
                                    Text(record.ref)
                                        .lineLimit(1)
                                        .truncationMode(.middle)
                                }
                                .buttonStyle(.link)
                            }
                            .font(.caption)
                        }
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                }
            }
        }
    }
}
