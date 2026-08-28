import NarumiMenuBarCore
import SwiftUI
import UniformTypeIdentifiers

/// コンテキスト tab: registered contexts (`get_meeting.contexts`) + registration form
/// (`register_context` — paste text / URL / file with drag & drop, optional auto regenerate).
struct ContextsTabView: View {
    @ObservedObject var model: MainWindowModel
    @State private var form = MainWindowModel.ContextForm()
    @State private var submitting = false

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 12) {
                registeredList
                Divider()
                registrationForm
            }
            .padding(12)
        }
        .onDrop(of: [.fileURL], isTargeted: nil) { providers in
            handleDrop(providers)
        }
    }

    @ViewBuilder
    private var registeredList: some View {
        Text("登録済みコンテキスト").font(.headline)
        if let contexts = model.detail?.contexts, !contexts.isEmpty {
            VStack(alignment: .leading, spacing: 4) {
                ForEach(contexts) { context in
                    HStack {
                        Text(context.label ?? context.contextID)
                            .lineLimit(1)
                        Text(context.sourceType)
                            .font(.caption)
                            .padding(.horizontal, 6)
                            .padding(.vertical, 1)
                            .background(.quaternary, in: Capsule())
                        Spacer()
                        Text(context.status)
                            .font(.caption)
                            .foregroundStyle(context.status == "failed" ? .red : .secondary)
                        Text(NarumiFormat.jstDateTime(context.registeredAt))
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }
            }
        } else {
            Text("まだ登録されていません。議事録の精度を上げるには、アジェンダや Notion AI 議事録などを登録してください。")
                .font(.caption)
                .foregroundStyle(.secondary)
        }
    }

    private var registrationForm: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("コンテキストを登録").font(.headline)
            Picker("入力方法", selection: $form.mode) {
                ForEach(MainWindowModel.ContextForm.Mode.allCases) { mode in
                    Text(mode.rawValue).tag(mode)
                }
            }
            .pickerStyle(.segmented)
            .frame(width: 320)
            .onChange(of: form.mode) {
                // Keep source_type in sync with the obvious defaults; the picker below can
                // still override it (e.g. pasted text that is a zoom_transcript).
                switch form.mode {
                case .text: form.sourceType = "text"
                case .url: form.sourceType = "url"
                case .file: form.sourceType = "file"
                }
            }

            switch form.mode {
            case .text:
                TextEditor(text: $form.text)
                    .font(.body)
                    .frame(minHeight: 120)
                    .overlay(RoundedRectangle(cornerRadius: 4).stroke(Color.secondary.opacity(0.3)))
            case .url:
                TextField("https://…", text: $form.url)
                    .textFieldStyle(.roundedBorder)
            case .file:
                HStack {
                    TextField("ファイルパス（ここへドロップも可）", text: $form.filePath)
                        .textFieldStyle(.roundedBorder)
                    Button("選択…") {
                        chooseFile()
                    }
                }
            }

            Picker("種類", selection: $form.sourceType) {
                ForEach(MainWindowModel.ContextForm.sourceTypes, id: \.self) { type in
                    Text(type).tag(type)
                }
            }
            .fixedSize()

            TextField("ラベル（任意）", text: $form.label)
                .textFieldStyle(.roundedBorder)
                .frame(width: 320)

            Toggle("登録後に議事録を再生成する", isOn: $form.autoRegenerate)
                .toggleStyle(.checkbox)
            if form.autoRegenerate, let config = model.detail?.config {
                MinutesGenerationDisclosure(
                    config: config, catalog: model.minutesModelCatalog, includesNewContext: true)
            }

            HStack {
                Button("登録") {
                    let draft = form
                    let expectedConfig = draft.autoRegenerate ? model.detail?.config : nil
                    let expectedMeetingID = model.detail?.meeting.meetingID
                    submitting = true
                    Task {
                        if await model.registerContext(
                            draft, expectedConfig: expectedConfig, expectedMeetingID: expectedMeetingID) {
                            form = MainWindowModel.ContextForm()
                        }
                        submitting = false
                    }
                }
                .disabled(submitting || regenerationBlocked)
                if submitting {
                    ProgressView().controlSize(.small)
                }
            }
        }
    }

    private var regenerationBlocked: Bool {
        guard form.autoRegenerate else { return false }
        guard let detail = model.detail, detail.meeting.meetingID == model.selectedMeetingID else { return true }
        guard detail.config.minutesModel != nil else { return false }
        return model.minutesModelCatalog.isLoading
            || model.minutesModelCatalog.validationMessage(for: ProcessingConfigurationForm(config: detail.config)) != nil
    }

    private func chooseFile() {
        let panel = NSOpenPanel()
        panel.canChooseFiles = true
        panel.canChooseDirectories = false
        panel.allowsMultipleSelection = false
        if panel.runModal() == .OK, let url = panel.url {
            form.mode = .file
            form.filePath = url.path
        }
    }

    private func handleDrop(_ providers: [NSItemProvider]) -> Bool {
        guard let provider = providers.first(where: { $0.hasItemConformingToTypeIdentifier(UTType.fileURL.identifier) }) else {
            return false
        }
        _ = provider.loadObject(ofClass: URL.self) { url, _ in
            guard let url, url.isFileURL else {
                return
            }
            Task { @MainActor in
                form.mode = .file
                form.sourceType = "file"
                form.filePath = url.path
            }
        }
        return true
    }
}
