import Foundation
import NarumiMenuBarCore

extension NarumiClient: MinutesModelCatalogClient {}
extension NarumiClient: TranscriptionModelCatalogClient {}

extension MainWindowModel {
    typealias MeetingConfigForm = MeetingConfigurationForm
    typealias ProfileForm = ProfileConfigurationForm

    func saveMeetingConfig(_ form: MeetingConfigForm) async -> Bool {
        guard let meetingID = selectedMeetingID, form.meetingID == meetingID,
            let detail, detail.meeting.meetingID == meetingID else {
            return false
        }
        configurationSaveGeneration &+= 1
        let saveGeneration = configurationSaveGeneration
        let sessionGeneration = desktopSession.connectionGeneration
        transcriptionRetry.invalidate()
        transcriptionRequestRecovery.invalidate()
        guard await validateProcessingSelection(form.processing, title: "会議設定を保存できません") else { return false }
        guard !Task.isCancelled, saveGeneration == configurationSaveGeneration,
            sessionGeneration == desktopSession.connectionGeneration, selectedMeetingID == meetingID else { return false }
        do {
            guard self.detail?.config == form.processing.originalConfig,
                self.detail?.meeting.scope == form.originalScope else {
                throw ConfigurationFormFailure(message: "編集中に会議設定が変わりました。最新の設定を読み込み直してください。")
            }
            let update = try form.processing.makeUpdate(
                supportsMinutesModel: supportsMinutesModels,
                supportedProviders: minutesModelCatalog.supportedProviders,
                supportsTranscriptionModel: supportsTranscriptionModels,
                supportedTranscriptionProviders: transcriptionModelCatalog.supportedProviders)
            var updates = try NarumiClient.arguments(update)
            let newScope = form.scopeText.trimmingCharacters(in: .whitespaces)
            if newScope != (form.originalScope ?? "") {
                updates["new_scope"] = newScope.isEmpty ? .null : .string(newScope)
            }
            let response = try await client.setMeetingConfig(
                meetingID: meetingID, scope: form.originalScope, updates: updates,
                expectedConfig: supportsTranscriptionModels ? form.processing.originalConfig : nil)
            guard !Task.isCancelled, saveGeneration == configurationSaveGeneration,
                sessionGeneration == desktopSession.connectionGeneration, selectedMeetingID == meetingID else { return false }
            guard response.meetingID == meetingID, response.scope == (newScope.isEmpty ? nil : newScope),
                response.config == update.applying(to: form.processing.originalConfig) else {
                throw ConfigurationFormFailure(message: "保存応答が確認した設定と一致しません。生成は開始していません。最新の設定を読み込み直してください。")
            }
            var saved = detail
            saved.config = response.config
            saved.meeting.scope = response.scope
            self.detail = saved
            showToast("会議設定を保存しました。音声送信・生成はまだ行っていません。反映には議事録タブで再生成してください。")
            await loadDetail()
            return true
        } catch {
            report(error, title: "会議設定を保存できません")
            return false
        }
    }

    func reloadProfiles() async {
        do { profilesList = try await client.profiles() }
        catch { report(error, title: "プロファイルを読み込めません") }
    }

    /// Fresh copy for the editor (get_profile; the row list may be a stale poll).
    func editProfile(name: String) async -> ProfileForm? {
        do { return ProfileForm(profile: try await client.profile(name: name)) }
        catch {
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
        configurationSaveGeneration &+= 1
        let saveGeneration = configurationSaveGeneration
        let sessionGeneration = desktopSession.connectionGeneration
        transcriptionRetry.invalidate()
        transcriptionRequestRecovery.invalidate()
        guard await validateProcessingSelection(form.processing, title: "プロファイルを保存できません") else { return false }
        guard !Task.isCancelled, saveGeneration == configurationSaveGeneration,
            sessionGeneration == desktopSession.connectionGeneration else { return false }
        do {
            let update = try form.processing.makeUpdate(
                supportsMinutesModel: supportsMinutesModels,
                supportedProviders: minutesModelCatalog.supportedProviders,
                supportsTranscriptionModel: supportsTranscriptionModels,
                supportedTranscriptionProviders: transcriptionModelCatalog.supportedProviders)
            let config = try NarumiClient.arguments(update)
            var updates: [String: JSONNode] = ["config": .object(config)]
            let scope = form.scope.trimmingCharacters(in: .whitespaces)
            updates["scope"] = scope.isEmpty ? .null : .string(scope)
            let engagement = form.engagement.trimmingCharacters(in: .whitespaces)
            updates["engagement"] = engagement.isEmpty ? .null : .string(engagement)
            updates["export_destinations"] = .array(form.exportDestinations.sorted().map(JSONNode.string))
            if form.makeDefault { updates["make_default"] = .bool(true) }
            let response = try await client.setProfile(
                name: name, updates: updates, expectedConfig: supportsTranscriptionModels ? form.expectedConfig : nil)
            guard !Task.isCancelled, saveGeneration == configurationSaveGeneration,
                sessionGeneration == desktopSession.connectionGeneration else { return false }
            guard response.name == name, response.config == update.applying(to: form.expectedConfig),
                response.scope == (scope.isEmpty ? nil : scope),
                response.engagement == (engagement.isEmpty ? nil : engagement),
                Set(response.exportDestinations) == form.exportDestinations,
                !form.makeDefault || response.isDefault else {
                throw ConfigurationFormFailure(message: "保存応答が確認したプロファイルと一致しません。音声送信・生成は行っていません。最新の設定を読み込み直してください。")
            }
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
        } catch { report(error, title: "プロファイルを削除できません") }
    }

    private func validateProcessingSelection(_ form: ProcessingConfigurationForm, title: String) async -> Bool {
        if form.minutesModel.mode == .selected {
            await minutesModelCatalog.loadCachedCatalog(
                connectionID: form.minutesModel.connectionID, selectedModelID: form.minutesModel.modelID)
        }
        guard !Task.isCancelled else { return false }
        if form.transcriptionModel.mode == .selected {
            await transcriptionModelCatalog.loadCachedCatalog(
                connectionID: form.transcriptionModel.connectionID, selectedModelID: form.transcriptionModel.modelID)
        }
        guard !Task.isCancelled else { return false }
        if let message = configurationValidationMessage(for: form) {
            alert = AlertContent(title: title, message: message)
            return false
        }
        return true
    }

    private var supportsMinutesModels: Bool {
        // Legacy operations remain available against an authenticated contract 2 server.
        guard let version = serverInfo?.contractVersion, RecordingPermissionContract.supportsSetup(version) else { return false }
        return ["3", "4", "5"].contains(String(version.split(separator: ".").first ?? ""))
    }

    var supportsTranscriptionModels: Bool {
        guard let version = serverInfo?.contractVersion, RecordingPermissionContract.supportsSetup(version) else { return false }
        return version.split(separator: ".").first == "5"
    }

    func configurationValidationMessage(for form: ProcessingConfigurationForm) -> String? {
        if form.minutesModel.mode == .selected {
            if minutesModelCatalog.isLoading { return "議事録モデルの情報を確認しています。" }
            if let message = minutesModelCatalog.validationMessage(for: form) { return message }
        }
        if form.transcriptionModel.mode == .selected {
            if transcriptionModelCatalog.isLoading { return "音声認識モデルの情報を確認しています。" }
            if let message = transcriptionModelCatalog.validationMessage(for: form) { return message }
        }
        return nil
    }

    func generationValidationMessage(config: MeetingConfig) -> String? {
        configurationValidationMessage(for: ProcessingConfigurationForm(config: config))
    }
}
