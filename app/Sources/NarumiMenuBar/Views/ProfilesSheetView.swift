import NarumiMenuBarCore
import SwiftUI

/// プロファイル sheet: `list_profiles` / `get_profile` / `set_profile` (make_default 含む) /
/// `delete_profile`. Profiles hold the defaults applied to new recordings and imports.
struct ProfilesSheetView: View {
    @ObservedObject var model: MainWindowModel
    @State private var form: MainWindowModel.ProfileForm?
    @State private var deleting: String?

    var body: some View {
        HStack(spacing: 0) {
            profileList
                .frame(width: 200)
            Divider()
            editor
                .frame(width: 420)
        }
        .frame(height: 520)
        .task {
            await model.reloadProfiles()
        }
    }

    private var profileList: some View {
        VStack(alignment: .leading, spacing: 0) {
            Text("プロファイル").font(.headline)
                .padding(10)
            List {
                ForEach(model.profilesList?.profiles ?? []) { profile in
                    HStack {
                        Text(profile.name)
                        if profile.isDefault {
                            Text("既定")
                                .font(.caption2)
                                .padding(.horizontal, 5)
                                .padding(.vertical, 1)
                                .background(.blue.opacity(0.2), in: Capsule())
                        }
                        Spacer()
                    }
                    .contentShape(Rectangle())
                    .onTapGesture {
                        Task {
                            form = await model.editProfile(name: profile.name)
                        }
                    }
                }
            }
            Divider()
            HStack {
                Button {
                    form = MainWindowModel.ProfileForm()
                } label: {
                    Label("新規", systemImage: "plus")
                }
                Spacer()
                Button("閉じる") {
                    model.showProfilesSheet = false
                }
                .keyboardShortcut(.cancelAction)
            }
            .padding(10)
        }
    }

    @ViewBuilder
    private var editor: some View {
        if form != nil {
            profileEditor
        } else {
            ContentUnavailableView(
                "プロファイルを選択", systemImage: "person.crop.circle",
                description: Text("左の一覧から編集するプロファイルを選ぶか、「新規」で作成します。"))
        }
    }

    private var capabilities: ServerCapabilities? { model.serverInfo?.capabilities }

    @ViewBuilder
    private var profileEditor: some View {
        let binding = Binding(
            get: { form ?? MainWindowModel.ProfileForm() },
            set: { form = $0 })
        VStack(spacing: 0) {
            Form {
                Section(binding.wrappedValue.isNew ? "新規プロファイル" : "プロファイル: \(binding.wrappedValue.name)") {
                    if binding.wrappedValue.isNew {
                        TextField("名前（必須）", text: binding.name)
                    }
                    capabilityPicker("文字起こしエンジン", selection: binding.transcriptionEngine,
                                     options: capabilities?.transcriptionEngines ?? [])
                    capabilityPicker("話者分離エンジン", selection: binding.diarizationEngine,
                                     options: capabilities?.diarizationEngines ?? [])
                    capabilityPicker("LLM プロバイダ", selection: binding.llmProvider,
                                     options: capabilities?.llmProviders ?? [])
                    Picker("外部送信ポリシー", selection: binding.externalSendPolicy) {
                        Text("（設定しない）").tag("")
                        Text("local_only").tag("local_only")
                        Text("subscription_ok").tag("subscription_ok")
                        Text("api_ok").tag("api_ok")
                    }
                    TextField("言語（ja / en など）", text: binding.language)
                    TextField("自分の名前", text: binding.selfName)
                    VStack(alignment: .leading) {
                        Text("語彙ヒント（1 行 1 語）")
                        TextEditor(text: binding.vocabHintsText)
                            .frame(minHeight: 60)
                            .overlay(RoundedRectangle(cornerRadius: 4).stroke(Color.secondary.opacity(0.3)))
                    }
                    TextField("既定 scope（任意）", text: binding.scope)
                    TextField("既定 engagement（任意）", text: binding.engagement)
                }
                Section("自動エクスポート先（処理完了時）") {
                    ForEach(model.exportDestinations) { destination in
                        Toggle(destination.name, isOn: Binding(
                            get: { binding.wrappedValue.exportDestinations.contains(destination.name) },
                            set: { on in
                                if on {
                                    form?.exportDestinations.insert(destination.name)
                                } else {
                                    form?.exportDestinations.remove(destination.name)
                                }
                            }))
                            .toggleStyle(.checkbox)
                    }
                }
                Section {
                    Toggle("既定プロファイルにする", isOn: binding.makeDefault)
                        .toggleStyle(.checkbox)
                }
            }
            .formStyle(.grouped)
            Divider()
            HStack {
                if !binding.wrappedValue.isNew {
                    Button("削除…", role: .destructive) {
                        deleting = binding.wrappedValue.name
                    }
                    .confirmationDialog(
                        "プロファイルを削除しますか？",
                        isPresented: Binding(get: { deleting != nil }, set: { if !$0 { deleting = nil } }),
                        titleVisibility: .visible
                    ) {
                        Button("削除する", role: .destructive) {
                            if let name = deleting {
                                Task {
                                    await model.deleteProfile(name: name)
                                    form = nil
                                }
                            }
                            deleting = nil
                        }
                        Button("キャンセル", role: .cancel) { deleting = nil }
                    } message: {
                        Text("既定プロファイルは削除できません。")
                    }
                }
                Spacer()
                Button("保存") {
                    Task {
                        if let current = form, await model.saveProfile(current) {
                            form = nil
                        }
                    }
                }
                .keyboardShortcut(.defaultAction)
            }
            .padding(10)
        }
    }

    private func capabilityPicker(_ title: String, selection: Binding<String>, options: [String]) -> some View {
        Picker(title, selection: selection) {
            Text("（設定しない）").tag("")
            let values = options.contains(selection.wrappedValue) || selection.wrappedValue.isEmpty
                ? options : options + [selection.wrappedValue]
            ForEach(values, id: \.self) { option in
                Text(option).tag(option)
            }
        }
    }
}
