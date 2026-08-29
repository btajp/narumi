import NarumiMenuBarCore
import SwiftUI

/// 設定 tab: `set_meeting_config` form (pickers fed by `get_server_info.capabilities`) and the
/// danger zone (`discard_tracks` / `delete_meeting`, both behind explicit confirmations).
struct SettingsTabView: View {
    @ObservedObject var model: MainWindowModel
    @State private var form = MainWindowModel.MeetingConfigForm()
    @State private var loadedMeetingID: String?
    @State private var discardScreen = false
    @State private var discardMic = false
    @State private var discardSystem = false
    @State private var showDiscardConfirm = false
    @State private var showDeleteConfirm = false
    @State private var saving = false

    var body: some View {
        Form {
            configSection
            dangerSection
        }
        .formStyle(.grouped)
        .onAppear {
            syncForm()
        }
        .onChange(of: model.detail?.meeting.meetingID) {
            loadedMeetingID = nil
            syncForm()
        }
    }

    /// (Re)seed the form from the loaded detail once per meeting so typing is not clobbered
    /// by the 5 s poll.
    private func syncForm() {
        guard let detail = model.detail, loadedMeetingID != detail.meeting.meetingID else {
            return
        }
        form = MainWindowModel.MeetingConfigForm(detail: detail)
        loadedMeetingID = detail.meeting.meetingID
        discardScreen = false
        discardMic = false
        discardSystem = false
    }

    private var capabilities: ServerCapabilities? { model.serverInfo?.capabilities }

    @ViewBuilder
    private var configSection: some View {
        Section("会議設定（保存後、反映には再生成が必要）") {
            ProcessingConfigurationFields(
                form: $form.processing, capabilities: capabilities, catalog: model.minutesModelCatalog,
                transcriptionCatalog: model.transcriptionModelCatalog)
            TextField("scope（空 = scope なし）", text: $form.scopeText)
            Button(saving ? "保存中…" : "保存") {
                let draft = form
                saving = true
                Task {
                    if await model.saveMeetingConfig(draft) {
                        loadedMeetingID = nil
                        syncForm()
                    }
                    saving = false
                }
            }
            .disabled(saving || loadedMeetingID != model.selectedMeetingID
                || model.configurationValidationMessage(for: form.processing) != nil)
            Button("保存済みの設定を読み直す") {
                let meetingID = model.selectedMeetingID
                Task {
                    await model.loadDetail()
                    guard model.selectedMeetingID == meetingID else { return }
                    loadedMeetingID = nil
                    syncForm()
                }
            }
            .help("入力中の変更を破棄し、現在保存されている設定を読み込みます。")
        }
        .disabled(saving)
    }

    @ViewBuilder
    private var dangerSection: some View {
        Section("危険な操作") {
            VStack(alignment: .leading, spacing: 6) {
                Text("録画トラックの破棄（文字起こし後に音声・動画を消して容量を空ける。sha256 は manifest に残る）")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                HStack(spacing: 16) {
                    trackToggle("screen", isOn: $discardScreen)
                    trackToggle("mic", isOn: $discardMic)
                    trackToggle("system", isOn: $discardSystem)
                }
                Button("選択したトラックを破棄…", role: .destructive) {
                    showDiscardConfirm = true
                }
                .disabled(selectedTracks.isEmpty)
                .confirmationDialog(
                    "トラックを破棄しますか？", isPresented: $showDiscardConfirm, titleVisibility: .visible
                ) {
                    Button("破棄する（\(selectedTracks.joined(separator: ", "))）", role: .destructive) {
                        Task {
                            await model.discardTracks(selectedTracks)
                            discardScreen = false
                            discardMic = false
                            discardSystem = false
                        }
                    }
                    Button("キャンセル", role: .cancel) {}
                } message: {
                    Text("破棄した音声・動画ファイルは元に戻せません。mic / system は対応する文字起こしがある場合のみ破棄できます。")
                }
            }

            VStack(alignment: .leading, spacing: 6) {
                Text("会議の削除（バンドルを trash/ へ移動し、一覧から除きます）")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                Button("この会議を削除…", role: .destructive) {
                    showDeleteConfirm = true
                }
                .confirmationDialog(
                    "会議を削除しますか？", isPresented: $showDeleteConfirm, titleVisibility: .visible
                ) {
                    Button("削除する", role: .destructive) {
                        Task { await model.deleteSelectedMeeting() }
                    }
                    Button("キャンセル", role: .cancel) {}
                } message: {
                    Text("「\(model.detail?.meeting.meetingName ?? "")」のバンドル一式を trash/ へ移動します。")
                }
            }
        }
    }

    private func trackToggle(_ name: String, isOn: Binding<Bool>) -> some View {
        let track = model.detail?.recording.tracks[name]
        let discarded = track?.discarded ?? false
        return Toggle(discarded ? "\(name)（破棄済み）" : name, isOn: isOn)
            .toggleStyle(.checkbox)
            .disabled(track == nil || discarded)
    }

    private var selectedTracks: [String] {
        var tracks: [String] = []
        if discardScreen { tracks.append("screen") }
        if discardMic { tracks.append("mic") }
        if discardSystem { tracks.append("system") }
        return tracks
    }
}
