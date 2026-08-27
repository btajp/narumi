import AppKit
import Combine
import Foundation
import NarumiMenuBarCore

/// State + actions of the main window. Every data operation is an MCP tool call through
/// `NarumiClient` (AGENTS.md 絶対原則 3); the only non-MCP conveniences are revealing paths the
/// tools returned in Finder and the server-process / update actions injected by the AppDelegate.
@MainActor
final class MainWindowModel: ObservableObject {
    struct AlertContent: Identifiable {
        let id = UUID()
        var title: String
        var message: String
    }

    enum DetailTab: String, CaseIterable, Identifiable {
        case minutes = "議事録"
        case transcript = "文字起こし"
        case contexts = "コンテキスト"
        case settings = "設定"
        var id: String { rawValue }
    }

    /// Actions that are app-specific by design (process management / Sparkle), injected by
    /// the AppDelegate. They are not data operations.
    struct HostActions {
        var restartServer: () -> Void = {}
        var openServerLog: () -> Void = {}
        var checkForUpdates: () -> Void = {}
        /// Lets the menu bar state (recording flag) follow a stop issued from the window.
        var recordingStopped: () -> Void = {}
    }

    let client: NarumiClient
    var hostActions = HostActions()

    // MARK: Sidebar

    @Published var scopeText = ""
    @Published var searchText = ""
    /// false = list_meetings free-text query; true = search_transcripts segment search.
    @Published var transcriptSearchEnabled = false
    @Published var meetings: [MeetingSummary] = []
    @Published var searchHits: [SearchHit] = []
    @Published var selectedMeetingID: String?
    @Published var lastRefreshError: String?

    // MARK: Recording banner

    @Published var recordingStatus = RecordingStatus(active: false)
    @Published var stoppingRecording = false

    // MARK: Detail

    @Published var selectedTab: DetailTab = .minutes
    @Published var detail: MeetingDetail?
    @Published var minutes: Minutes?
    @Published var selectedMinutesVersion: Int?
    @Published var minutesUnavailable: String?
    @Published var transcript: Transcript?
    @Published var selectedTranscriptSource: String?
    @Published var transcriptUnavailable: String?

    // MARK: Server-wide data

    @Published var serverInfo: ServerInfo?
    @Published var exportDestinations: [ExportDestinationInfo] = []
    @Published var profilesList: ListProfilesResponse?
    @Published var rebuildResult: RebuildCatalogResponse?

    // MARK: Jobs

    @Published var jobs: [Job] = []
    private var trackedJobIDs: [String] = []
    private var lastJobStatuses: [String: String] = [:]

    // MARK: Feedback / sheets

    @Published var alert: AlertContent?
    @Published var toast: String?
    private var toastGeneration = 0
    @Published var showImportSheet = false
    @Published var showProfilesSheet = false
    @Published var showDiagnosticsSheet = false

    init(client: NarumiClient) {
        self.client = client
    }

    // MARK: Polling lifecycle (owned by the window controller)

    private var pollTask: Task<Void, Never>?

    var isPolling: Bool { pollTask != nil }

    /// Start the 5 s refresh loop while the window is visible. Idempotent.
    func startPolling(interval: Duration = .seconds(5)) {
        guard pollTask == nil else {
            return
        }
        pollTask = Task { [weak self] in
            guard let self else {
                return
            }
            await self.initialLoad()
            while !Task.isCancelled {
                try? await Task.sleep(for: interval)
                guard !Task.isCancelled else {
                    return
                }
                await self.refresh()
            }
        }
    }

    /// Stop refreshing when the window closes (the app returns to the menu bar only).
    func stopPolling() {
        pollTask?.cancel()
        pollTask = nil
    }

    var scopeValues: [String] { NarumiFormat.parseScopeInput(scopeText) }

    /// Scope selector value for operations on one meeting: the meeting's own scope.
    private func scopeFor(meetingID: String) -> String? {
        if let detail, detail.meeting.meetingID == meetingID {
            return detail.meeting.scope
        }
        return meetings.first(where: { $0.meetingID == meetingID })?.scope
    }

    var showsSearchHits: Bool {
        transcriptSearchEnabled && !searchText.trimmingCharacters(in: .whitespaces).isEmpty
    }

    // MARK: Feedback

    func report(_ error: Error, title: String) {
        if let failure = error as? ToolFailure {
            if failure.isBusy {
                showToast("実行中のジョブがあるため実行できません: \(failure.message)")
                return
            }
            alert = AlertContent(title: title, message: failure.description)
        } else {
            alert = AlertContent(title: title, message: error.localizedDescription)
        }
    }

    func showToast(_ text: String) {
        toast = text
        toastGeneration += 1
        let generation = toastGeneration
        Task { [weak self] in
            try? await Task.sleep(for: .seconds(5))
            guard let self, self.toastGeneration == generation else {
                return
            }
            self.toast = nil
        }
    }

    // MARK: Refresh (5 s tick while the window is open)

    func initialLoad() async {
        await refreshServerWideData()
        await refresh()
    }

    func refreshServerWideData() async {
        do {
            serverInfo = try await client.serverInfo()
        } catch {
            lastRefreshError = (error as? ToolFailure)?.message ?? error.localizedDescription
        }
        do {
            exportDestinations = try await client.exportDestinations()
        } catch {
            // Non-fatal: the export menu just stays empty until the next open.
        }
        do {
            profilesList = try await client.profiles()
        } catch {
            // Non-fatal: profile pickers fall back to free text.
        }
    }

    func refresh() async {
        let query = searchText.trimmingCharacters(in: .whitespaces)
        do {
            if showsSearchHits {
                searchHits = try await client.searchTranscripts(query: query, scope: scopeValues, limit: 50)
            } else {
                meetings = try await client.listMeetings(
                    query: query.isEmpty ? nil : query, scope: scopeValues)
                searchHits = []
            }
            lastRefreshError = nil
        } catch {
            lastRefreshError = (error as? ToolFailure)?.message ?? error.localizedDescription
        }
        do {
            recordingStatus = try await client.recordingStatus()
        } catch {
            recordingStatus = RecordingStatus(active: false)
        }
        await refreshJobs()
    }

    // MARK: Selection / detail

    func selectionChanged() async {
        detail = nil
        minutes = nil
        minutesUnavailable = nil
        selectedMinutesVersion = nil
        transcript = nil
        selectedTranscriptSource = nil
        transcriptUnavailable = nil
        guard selectedMeetingID != nil else {
            return
        }
        await loadDetail()
        await loadTabContent()
    }

    func loadDetail() async {
        guard let meetingID = selectedMeetingID else {
            return
        }
        do {
            let loaded = try await client.meeting(id: meetingID, scope: scopeFor(meetingID: meetingID))
            if selectedMeetingID == meetingID {
                detail = loaded
            }
        } catch {
            report(error, title: "会議を読み込めません")
        }
    }

    func tabChanged() async {
        await loadTabContent()
    }

    private func loadTabContent() async {
        switch selectedTab {
        case .minutes:
            if minutes == nil {
                await loadMinutes()
            }
        case .transcript:
            if transcript == nil {
                await loadTranscript()
            }
        case .contexts, .settings:
            break
        }
    }

    func loadMinutes() async {
        guard let meetingID = selectedMeetingID else {
            return
        }
        do {
            let loaded = try await client.minutes(
                meetingID: meetingID, version: selectedMinutesVersion,
                scope: scopeFor(meetingID: meetingID))
            guard selectedMeetingID == meetingID else {
                return
            }
            minutes = loaded
            selectedMinutesVersion = loaded.version
            minutesUnavailable = nil
        } catch let failure as ToolFailure where failure.code == "not_found" {
            minutes = nil
            minutesUnavailable = "議事録はまだ生成されていません（処理ジョブの完了後に表示されます）"
        } catch {
            report(error, title: "議事録を読み込めません")
        }
    }

    func minutesVersionChanged(_ version: Int) async {
        selectedMinutesVersion = version
        await loadMinutes()
    }

    func loadTranscript() async {
        guard let meetingID = selectedMeetingID else {
            return
        }
        do {
            let loaded = try await client.transcript(
                meetingID: meetingID, source: selectedTranscriptSource,
                scope: scopeFor(meetingID: meetingID))
            guard selectedMeetingID == meetingID else {
                return
            }
            transcript = loaded
            selectedTranscriptSource = loaded.source
            transcriptUnavailable = nil
        } catch let failure as ToolFailure where failure.code == "not_found" {
            transcript = nil
            transcriptUnavailable = "文字起こしはまだありません（処理ジョブの完了後に表示されます）"
        } catch {
            report(error, title: "文字起こしを読み込めません")
        }
    }

    func transcriptSourceChanged(_ source: String) async {
        selectedTranscriptSource = source
        await loadTranscript()
    }

    /// A search hit opens its meeting on the 文字起こし tab at the hit's source.
    func openSearchHit(_ hit: SearchHit) async {
        selectedTab = .transcript
        selectedMeetingID = hit.meetingID
        await selectionChanged()
        if transcript?.availableSources.contains(hit.sourceID) == true {
            await transcriptSourceChanged(hit.sourceID)
        }
    }

    // MARK: Recording banner

    func stopRecordingFromBanner() async {
        stoppingRecording = true
        defer { stoppingRecording = false }
        do {
            try await client.stopRecording()
            hostActions.recordingStopped()
            showToast("録画を停止しました（処理ジョブを開始）")
            await refresh()
        } catch {
            report(error, title: "録画を停止できません")
        }
    }

    // MARK: Minutes actions

    func regenerate(force: Bool, reason: String) async {
        guard let meetingID = selectedMeetingID else {
            return
        }
        do {
            let response = try await client.regenerate(
                meetingID: meetingID, scope: scopeFor(meetingID: meetingID), force: force,
                reason: reason.trimmingCharacters(in: .whitespaces))
            track(jobID: response.jobID)
            showToast("再生成ジョブを開始しました (\(response.jobID))")
        } catch {
            report(error, title: "再生成を開始できません")
        }
    }

    /// File exporters (options_schema with output_path) go through NSSavePanel; the panel's
    /// own replace confirmation covers overwrite=true.
    func export(destination: ExportDestinationInfo) async {
        guard let meetingID = selectedMeetingID else {
            return
        }
        let version = selectedMinutesVersion ?? minutes?.version
        var options: [String: JSONNode]?
        if let ext = Self.fileExtension(forDestination: destination.name) {
            NSApp.activate(ignoringOtherApps: true)
            let panel = NSSavePanel()
            panel.canCreateDirectories = true
            panel.nameFieldStringValue = "\(meetingID)-v\(version.map(String.init) ?? "latest").\(ext)"
            panel.title = "議事録をエクスポート"
            guard panel.runModal() == .OK, let url = panel.url else {
                return
            }
            options = ["output_path": .string(url.path), "overwrite": .bool(true)]
        }
        do {
            let response = try await client.exportMinutes(
                meetingID: meetingID, scope: scopeFor(meetingID: meetingID),
                destination: destination.name, options: options, minutesVersion: version)
            if let result = response.result {
                showToast("エクスポート完了: \(result.ref)")
                revealRef(result.ref)
                await loadDetail()
            } else if let jobID = response.jobID {
                track(jobID: jobID)
                showToast("エクスポートジョブを開始しました (\(jobID))")
            }
        } catch {
            report(error, title: "エクスポートできません")
        }
    }

    /// Exporters whose ref is a local file (their output path). Others (URL / page id) get no
    /// save panel and no Finder reveal.
    static func fileExtension(forDestination name: String) -> String? {
        switch name {
        case "markdown": return "md"
        case "html": return "html"
        default: return nil
        }
    }

    /// Reveal a tool-returned absolute path in Finder (allowed non-MCP convenience).
    func revealRef(_ ref: String) {
        guard ref.hasPrefix("/"), FileManager.default.fileExists(atPath: ref) else {
            return
        }
        NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: ref)])
    }

    func openBundleInFinder() {
        guard let path = detail?.bundlePath else {
            return
        }
        NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: path)])
    }

    // MARK: Context actions

    struct ContextForm {
        enum Mode: String, CaseIterable, Identifiable {
            case text = "テキスト"
            case url = "URL"
            case file = "ファイル"
            var id: String { rawValue }
        }

        var mode: Mode = .text
        var sourceType = "text"
        var text = ""
        var url = ""
        var filePath = ""
        var label = ""
        var autoRegenerate = false

        static let sourceTypes = [
            "text", "url", "file", "document", "chat_log",
            "notion_ai_minutes", "zoom_transcript", "meet_transcript", "teams_transcript",
        ]
    }

    func registerContext(_ form: ContextForm) async -> Bool {
        guard let meetingID = selectedMeetingID else {
            return false
        }
        let payload: NarumiClient.ContextPayload
        switch form.mode {
        case .text:
            let text = form.text
            guard !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
                alert = AlertContent(title: "コンテキストを登録できません", message: "テキストが空です。")
                return false
            }
            payload = .content(text)
        case .url:
            let url = form.url.trimmingCharacters(in: .whitespaces)
            guard url.hasPrefix("http://") || url.hasPrefix("https://") else {
                alert = AlertContent(title: "コンテキストを登録できません", message: "http(s) URL を入力してください。")
                return false
            }
            payload = .url(url)
        case .file:
            let path = form.filePath.trimmingCharacters(in: .whitespaces)
            guard path.hasPrefix("/") else {
                alert = AlertContent(title: "コンテキストを登録できません", message: "ファイルを選択（またはドロップ）してください。")
                return false
            }
            payload = .filePath(path)
        }
        do {
            let response = try await client.registerContext(
                meetingID: meetingID, scope: scopeFor(meetingID: meetingID),
                sourceType: form.sourceType, payload: payload, label: form.label,
                autoRegenerate: form.autoRegenerate)
            if let jobID = response.jobID {
                track(jobID: jobID)
            }
            showToast("コンテキストを登録しました (\(response.contextID), \(response.status))")
            await loadDetail()
            return true
        } catch {
            report(error, title: "コンテキストを登録できません")
            return false
        }
    }

    // MARK: Settings actions

    struct MeetingConfigForm {
        var transcriptionEngine = ""
        var diarizationEngine = ""
        var llmProvider = ""
        var externalSendPolicy = ""
        var language = ""
        var selfName = ""
        var vocabHintsText = ""
        var scopeText = ""

        init() {}

        init(detail: MeetingDetail) {
            transcriptionEngine = detail.config.transcriptionEngine ?? ""
            diarizationEngine = detail.config.diarizationEngine ?? ""
            llmProvider = detail.config.llmProvider ?? ""
            externalSendPolicy = detail.config.externalSendPolicy ?? ""
            language = detail.config.language ?? ""
            selfName = detail.config.selfName ?? ""
            vocabHintsText = (detail.config.vocabHints ?? []).joined(separator: "\n")
            scopeText = detail.meeting.scope ?? ""
        }

        var vocabHints: [String] {
            vocabHintsText.split(whereSeparator: \.isNewline)
                .map { $0.trimmingCharacters(in: .whitespaces) }
                .filter { !$0.isEmpty }
        }
    }

    func saveMeetingConfig(_ form: MeetingConfigForm) async {
        guard let meetingID = selectedMeetingID, let detail else {
            return
        }
        var updates: [String: JSONNode] = [:]
        if !form.transcriptionEngine.isEmpty {
            updates["transcription_engine"] = .string(form.transcriptionEngine)
        }
        if !form.diarizationEngine.isEmpty {
            updates["diarization_engine"] = .string(form.diarizationEngine)
        }
        if !form.llmProvider.isEmpty {
            updates["llm_provider"] = .string(form.llmProvider)
        }
        if !form.externalSendPolicy.isEmpty {
            updates["external_send_policy"] = .string(form.externalSendPolicy)
        }
        if !form.language.isEmpty {
            updates["language"] = .string(form.language)
        }
        let selfName = form.selfName.trimmingCharacters(in: .whitespaces)
        updates["self_name"] = selfName.isEmpty ? .null : .string(selfName)
        updates["vocab_hints"] = .array(form.vocabHints.map(JSONNode.string))
        let newScope = form.scopeText.trimmingCharacters(in: .whitespaces)
        if newScope != (detail.meeting.scope ?? "") {
            updates["new_scope"] = newScope.isEmpty ? .null : .string(newScope)
        }
        do {
            _ = try await client.setMeetingConfig(
                meetingID: meetingID, scope: detail.meeting.scope, updates: updates)
            showToast("会議設定を保存しました（反映には再生成が必要です）")
            await loadDetail()
        } catch {
            report(error, title: "会議設定を保存できません")
        }
    }

    func discardTracks(_ tracks: [String]) async {
        guard let meetingID = selectedMeetingID, !tracks.isEmpty else {
            return
        }
        do {
            _ = try await client.discardTracks(
                meetingID: meetingID, tracks: tracks, scope: scopeFor(meetingID: meetingID))
            showToast("トラックを破棄しました: \(tracks.joined(separator: ", "))")
            await loadDetail()
        } catch {
            report(error, title: "トラックを破棄できません")
        }
    }

    func deleteSelectedMeeting() async {
        guard let meetingID = selectedMeetingID else {
            return
        }
        do {
            let response = try await client.deleteMeeting(
                meetingID: meetingID, scope: scopeFor(meetingID: meetingID))
            showToast("会議を削除しました（\(response.movedTo) へ移動）")
            selectedMeetingID = nil
            await selectionChanged()
            await refresh()
        } catch {
            report(error, title: "会議を削除できません")
        }
    }

    // MARK: Import

    struct ImportForm {
        var meetingName = ""
        var micPath = ""
        var systemPath = ""
        var screenPath = ""
        var scope = ""
        var profile = ""
        var copy = true
        var autoProcess = true

        var isSubmittable: Bool {
            !meetingName.trimmingCharacters(in: .whitespaces).isEmpty
                && (!micPath.isEmpty || !systemPath.isEmpty)
        }
    }

    func submitImport(_ form: ImportForm) async -> Bool {
        do {
            let response = try await client.importRecording(
                NarumiClient.ImportRequest(
                    meetingName: form.meetingName.trimmingCharacters(in: .whitespaces),
                    micPath: form.micPath.isEmpty ? nil : form.micPath,
                    systemPath: form.systemPath.isEmpty ? nil : form.systemPath,
                    screenPath: form.screenPath.isEmpty ? nil : form.screenPath,
                    scope: form.scope.trimmingCharacters(in: .whitespaces),
                    profile: form.profile,
                    copy: form.copy,
                    autoProcess: form.autoProcess))
            if let jobID = response.jobID {
                track(jobID: jobID)
            }
            showToast("取り込みました: \(response.meetingID)")
            if let scope = form.scope.trimmingCharacters(in: .whitespaces).nilIfEmpty,
                !scopeValues.contains(scope) {
                // Make the imported meeting visible in the list (scope selector is default deny).
                scopeText = scopeText.isEmpty ? scope : "\(scopeText) \(scope)"
            }
            await refresh()
            selectedMeetingID = response.meetingID
            await selectionChanged()
            return true
        } catch {
            report(error, title: "取り込みできません")
            return false
        }
    }

    // MARK: Profiles

    struct ProfileForm {
        var name = ""
        var transcriptionEngine = ""
        var diarizationEngine = ""
        var llmProvider = ""
        var externalSendPolicy = ""
        var language = ""
        var selfName = ""
        var vocabHintsText = ""
        var scope = ""
        var engagement = ""
        var exportDestinations: Set<String> = []
        var makeDefault = false
        var isNew = true

        init() {}

        init(profile: Profile) {
            name = profile.name
            transcriptionEngine = profile.config.transcriptionEngine ?? ""
            diarizationEngine = profile.config.diarizationEngine ?? ""
            llmProvider = profile.config.llmProvider ?? ""
            externalSendPolicy = profile.config.externalSendPolicy ?? ""
            language = profile.config.language ?? ""
            selfName = profile.config.selfName ?? ""
            vocabHintsText = (profile.config.vocabHints ?? []).joined(separator: "\n")
            scope = profile.scope ?? ""
            engagement = profile.engagement ?? ""
            exportDestinations = Set(profile.exportDestinations)
            makeDefault = profile.isDefault
            isNew = false
        }

        var vocabHints: [String] {
            vocabHintsText.split(whereSeparator: \.isNewline)
                .map { $0.trimmingCharacters(in: .whitespaces) }
                .filter { !$0.isEmpty }
        }
    }

    func reloadProfiles() async {
        do {
            profilesList = try await client.profiles()
        } catch {
            report(error, title: "プロファイルを読み込めません")
        }
    }

    /// Fresh copy for the editor (get_profile; the row list may be a stale 5 s poll).
    func editProfile(name: String) async -> ProfileForm? {
        do {
            return ProfileForm(profile: try await client.profile(name: name))
        } catch {
            report(error, title: "プロファイルを読み込めません")
            return nil
        }
    }

    func saveProfile(_ form: ProfileForm) async -> Bool {
        let name = form.name.trimmingCharacters(in: .whitespaces)
        guard !name.isEmpty else {
            alert = AlertContent(title: "プロファイルを保存できません", message: "名前を入力してください。")
            return false
        }
        var config: [String: JSONNode] = [:]
        if !form.transcriptionEngine.isEmpty {
            config["transcription_engine"] = .string(form.transcriptionEngine)
        }
        if !form.diarizationEngine.isEmpty {
            config["diarization_engine"] = .string(form.diarizationEngine)
        }
        if !form.llmProvider.isEmpty {
            config["llm_provider"] = .string(form.llmProvider)
        }
        if !form.externalSendPolicy.isEmpty {
            config["external_send_policy"] = .string(form.externalSendPolicy)
        }
        if !form.language.isEmpty {
            config["language"] = .string(form.language)
        }
        let selfName = form.selfName.trimmingCharacters(in: .whitespaces)
        config["self_name"] = selfName.isEmpty ? .null : .string(selfName)
        config["vocab_hints"] = .array(form.vocabHints.map(JSONNode.string))

        var updates: [String: JSONNode] = ["config": .object(config)]
        let scope = form.scope.trimmingCharacters(in: .whitespaces)
        updates["scope"] = scope.isEmpty ? .null : .string(scope)
        let engagement = form.engagement.trimmingCharacters(in: .whitespaces)
        updates["engagement"] = engagement.isEmpty ? .null : .string(engagement)
        updates["export_destinations"] = .array(form.exportDestinations.sorted().map(JSONNode.string))
        if form.makeDefault {
            updates["make_default"] = .bool(true)
        }
        do {
            _ = try await client.setProfile(name: name, updates: updates)
            showToast("プロファイルを保存しました: \(name)")
            await reloadProfiles()
            return true
        } catch {
            report(error, title: "プロファイルを保存できません")
            return false
        }
    }

    func deleteProfile(name: String) async {
        do {
            _ = try await client.deleteProfile(name: name)
            showToast("プロファイルを削除しました: \(name)")
            await reloadProfiles()
        } catch {
            report(error, title: "プロファイルを削除できません")
        }
    }

    // MARK: Diagnostics

    func rebuildCatalog() async {
        do {
            let result = try await client.rebuildCatalog()
            rebuildResult = result
            showToast("カタログを再構築しました（会議 \(result.meetings) 件 / セグメント \(result.segments) 件）")
            await refresh()
        } catch {
            report(error, title: "カタログを再構築できません")
        }
    }

    // MARK: Jobs

    func track(jobID: String) {
        if !trackedJobIDs.contains(jobID) {
            trackedJobIDs.insert(jobID, at: 0)
        }
        Task { await refreshJobs() }
    }

    var activeJobCount: Int { jobs.filter(\.isActive).count }

    private func refreshJobs() async {
        // Jobs the window started + the queued/running jobs the list reported.
        var ids = trackedJobIDs
        for meeting in meetings {
            if let job = meeting.activeJob, !ids.contains(job.jobID) {
                ids.append(job.jobID)
                trackedJobIDs.append(job.jobID)
            }
        }
        var refreshed: [Job] = []
        var selectedMeetingJobFinished = false
        for id in ids {
            guard let job = try? await client.jobStatus(jobID: id) else {
                continue  // expired / unknown: drop from the list
            }
            refreshed.append(job)
            let previous = lastJobStatuses[id]
            lastJobStatuses[id] = job.status
            if let previous, previous != job.status, !job.isActive,
                job.meetingID == selectedMeetingID {
                selectedMeetingJobFinished = true
            }
        }
        trackedJobIDs = refreshed.map(\.jobID)
        if trackedJobIDs.count > 20 {
            // Keep the newest 20; prune only finished jobs from the tail.
            let keep = refreshed.prefix(20).map(\.jobID) + refreshed.dropFirst(20).filter(\.isActive).map(\.jobID)
            trackedJobIDs = Array(keep)
            refreshed = refreshed.filter { trackedJobIDs.contains($0.jobID) }
        }
        jobs = refreshed
        if selectedMeetingJobFinished {
            await loadDetail()
            minutes = nil
            transcript = nil
            await loadTabContent()
        }
    }

    func cancel(jobID: String) async {
        do {
            let job = try await client.cancelJob(jobID: jobID)
            showToast("ジョブをキャンセルしました (\(job.jobID): \(NarumiFormat.jobStatusLabel(job.status)))")
            await refreshJobs()
        } catch {
            report(error, title: "ジョブをキャンセルできません")
        }
    }

    func clearFinishedJobs() {
        trackedJobIDs = jobs.filter(\.isActive).map(\.jobID)
        jobs = jobs.filter(\.isActive)
    }
}

extension String {
    fileprivate var nilIfEmpty: String? { isEmpty ? nil : self }
}
