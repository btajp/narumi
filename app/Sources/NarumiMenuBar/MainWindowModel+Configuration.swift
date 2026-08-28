import Foundation
import NarumiMenuBarCore

extension NarumiClient: MinutesModelCatalogClient {}

extension MainWindowModel {
    typealias MeetingConfigForm = MeetingConfigurationForm
    typealias ProfileForm = ProfileConfigurationForm

    func saveMeetingConfig(_ form: MeetingConfigForm) async -> Bool {
        guard let meetingID = selectedMeetingID, let detail, detail.meeting.meetingID == meetingID else {
            return false
        }
        guard await validateMinutesSelection(form.processing, title: "会議設定を保存できません") else { return false }
        do {
            var updates = try NarumiClient.arguments(form.processing.makeUpdate(supportsMinutesModel: supportsMinutesModels))
            let newScope = form.scopeText.trimmingCharacters(in: .whitespaces)
            if newScope != (detail.meeting.scope ?? "") {
                updates["new_scope"] = newScope.isEmpty ? .null : .string(newScope)
            }
            _ = try await client.setMeetingConfig(meetingID: meetingID, scope: detail.meeting.scope, updates: updates)
            showToast("会議設定を保存しました。まだ生成していません。議事録タブで再生成してください。")
            if selectedMeetingID == meetingID { await loadDetail() }
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
        guard await validateMinutesSelection(form.processing, title: "プロファイルを保存できません") else { return false }
        do {
            let config = try NarumiClient.arguments(form.processing.makeUpdate(supportsMinutesModel: supportsMinutesModels))
            var updates: [String: JSONNode] = ["config": .object(config)]
            let scope = form.scope.trimmingCharacters(in: .whitespaces)
            updates["scope"] = scope.isEmpty ? .null : .string(scope)
            let engagement = form.engagement.trimmingCharacters(in: .whitespaces)
            updates["engagement"] = engagement.isEmpty ? .null : .string(engagement)
            updates["export_destinations"] = .array(form.exportDestinations.sorted().map(JSONNode.string))
            if form.makeDefault { updates["make_default"] = .bool(true) }
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
        } catch { report(error, title: "プロファイルを削除できません") }
    }

    private func validateMinutesSelection(_ form: ProcessingConfigurationForm, title: String) async -> Bool {
        guard form.minutesModel.mode == .codex else { return true }
        await minutesModelCatalog.loadCachedCatalog(
            connectionID: form.minutesModel.connectionID, selectedModelID: form.minutesModel.modelID)
        guard !Task.isCancelled else { return false }
        if let message = minutesModelCatalog.validationMessage(for: form) {
            alert = AlertContent(title: title, message: message)
            return false
        }
        return true
    }

    private var supportsMinutesModels: Bool {
        // Legacy operations remain available against an authenticated contract 2 server.
        serverInfo?.contractVersion.split(separator: ".").first != "2"
    }
}
