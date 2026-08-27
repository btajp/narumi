import NarumiMenuBarCore
import SwiftUI

/// 取り込み sheet: `import_recording` for existing recordings (Zoom local recordings etc.),
/// with the profile picker from `list_profiles`.
struct ImportSheetView: View {
    @ObservedObject var model: MainWindowModel
    @State private var form = MainWindowModel.ImportForm()
    @State private var submitting = false

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Text("既存録画を取り込む").font(.title3.bold())
                .padding(12)
            Divider()
            Form {
                TextField("会議名（必須）", text: $form.meetingName)
                pathRow("マイク音声", path: $form.micPath)
                pathRow("システム音声（相手側）", path: $form.systemPath)
                pathRow("画面録画（任意）", path: $form.screenPath)
                Text("マイク音声かシステム音声のどちらかは必須です（1 本しかない録音はシステム音声へ）。")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                Picker("プロファイル", selection: $form.profile) {
                    Text("（既定プロファイル）").tag("")
                    ForEach(model.profilesList?.profiles ?? []) { profile in
                        Text(profile.isDefault ? "\(profile.name)（既定）" : profile.name)
                            .tag(profile.name)
                    }
                }
                TextField("scope（任意）", text: $form.scope)
                Toggle("ファイルをコピーする（オフ = ハードリンク）", isOn: $form.copy)
                Toggle("取り込み後すぐ処理する（文字起こし〜議事録生成）", isOn: $form.autoProcess)
            }
            .formStyle(.grouped)
            Divider()
            HStack {
                if submitting {
                    ProgressView().controlSize(.small)
                    Text("取り込み中…").font(.caption)
                }
                Spacer()
                Button("キャンセル") {
                    model.showImportSheet = false
                }
                .keyboardShortcut(.cancelAction)
                Button("取り込む") {
                    submitting = true
                    Task {
                        if await model.submitImport(form) {
                            model.showImportSheet = false
                            form = MainWindowModel.ImportForm()
                        }
                        submitting = false
                    }
                }
                .keyboardShortcut(.defaultAction)
                .disabled(!form.isSubmittable || submitting)
            }
            .padding(12)
        }
        .frame(width: 560)
    }

    private func pathRow(_ title: String, path: Binding<String>) -> some View {
        HStack {
            TextField(title, text: path)
            Button("選択…") {
                let panel = NSOpenPanel()
                panel.canChooseFiles = true
                panel.canChooseDirectories = false
                panel.allowsMultipleSelection = false
                if panel.runModal() == .OK, let url = panel.url {
                    path.wrappedValue = url.path
                }
            }
        }
    }
}
