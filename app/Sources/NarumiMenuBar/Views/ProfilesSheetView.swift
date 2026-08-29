import NarumiMenuBarCore
import SwiftUI

/// プロファイル sheet: `list_profiles` / `get_profile` / `set_profile` (make_default 含む) /
/// `delete_profile`. Profiles hold the defaults applied to new recordings and imports.
struct ProfilesSheetView: View {
    @ObservedObject var model: MainWindowModel
    @State private var form: MainWindowModel.ProfileForm?
    @State private var deleting: String?
    @State private var saving = false
    @State private var selectionGeneration: UInt64 = 0
    @State private var loadingProfile = false
    @State private var minutesValidationMessage: String?

    var body: some View {
        HStack(spacing: 0) {
            profileList
                .frame(width: 200)
            Divider()
            editor
                .frame(width: 560)
        }
        .frame(height: 660)
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
                        guard !saving else { return }
                        selectionGeneration &+= 1
                        let generation = selectionGeneration
                        loadingProfile = true
                        minutesValidationMessage = nil
                        Task {
                            let loaded = await model.editProfile(name: profile.name)
                            guard selectionGeneration == generation else { return }
                            form = loaded
                            loadingProfile = false
                        }
                    }
                }
            }
            Divider()
            HStack {
                Button {
                    selectionGeneration &+= 1
                    loadingProfile = false
                    minutesValidationMessage = nil
                    form = MainWindowModel.ProfileForm()
                } label: {
                    Label("新規", systemImage: "plus")
                }
                .disabled(saving)
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
                    ProcessingConfigurationFields(
                        form: binding.processing, capabilities: capabilities,
                        contractVersion: model.serverInfo?.contractVersion,
                        catalog: model.minutesModelCatalog, transcriptionCatalog: model.transcriptionModelCatalog,
                        isProfile: true, minutesValidationMessage: $minutesValidationMessage)
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
            .disabled(saving || loadingProfile)
            Divider()
            HStack {
                if !binding.wrappedValue.isNew {
                    Button("削除…", role: .destructive) {
                        deleting = binding.wrappedValue.name
                    }
                    .disabled(saving || loadingProfile)
                    .confirmationDialog(
                        "プロファイルを削除しますか？",
                        isPresented: Binding(get: { deleting != nil }, set: { if !$0 { deleting = nil } }),
                        titleVisibility: .visible
                    ) {
                        Button("削除する", role: .destructive) {
                            if let name = deleting {
                                saving = true
                                selectionGeneration &+= 1
                                Task {
                                    await model.deleteProfile(name: name)
                                    form = nil
                                    minutesValidationMessage = nil
                                    saving = false
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
                Button(saving ? "保存中…" : "保存") {
                    guard let current = form else { return }
                    saving = true
                    selectionGeneration &+= 1
                    Task {
                        if await model.saveProfile(current) { form = nil }
                        saving = false
                    }
                }
                .disabled(saving || loadingProfile || binding.wrappedValue.name.trimmingCharacters(in: .whitespaces).isEmpty
                    || minutesValidationMessage != nil
                    || model.configurationValidationMessage(for: binding.wrappedValue.processing) != nil)
                .keyboardShortcut(.defaultAction)
            }
            .padding(10)
        }
    }

}
