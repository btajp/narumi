import Foundation
import NarumiMenuBarCore

extension MainWindowModel {
    var transcriptionUnknownJob: Job? {
        guard let selectedMeetingID else { return nil }
        let latest = jobs.filter {
            $0.meetingID == selectedMeetingID && ["process", "regenerate"].contains($0.kind)
        }.max { $0.createdAt < $1.createdAt }
        guard let latest, !latest.isActive, latest.error?.transcriptionOutcome != nil else { return nil }
        return latest
    }

    func prepareTranscriptionRetry(job: Job) -> TranscriptionRetryConfirmation? {
        guard supportsTranscriptionModels, transcriptionModelCatalog.supportedProviders.contains("openai-api"),
            let detail, detail.meeting.meetingID == selectedMeetingID,
            job.meetingID == selectedMeetingID else { return nil }
        do {
            return try transcriptionRetry.prepare(meeting: detail, job: job)
        } catch {
            report(error, title: "音声認識の再送を確認できません")
            return nil
        }
    }

    func confirmTranscriptionRetry(_ confirmation: TranscriptionRetryConfirmation) async -> Bool {
        guard supportsTranscriptionModels, confirmation.meetingID == selectedMeetingID,
            detail?.config == confirmation.config, detail?.meeting.scope == confirmation.scope else {
            transcriptionRetry.invalidate()
            return false
        }
        if let message = generationValidationMessage(config: confirmation.config) {
            transcriptionRetry.invalidate()
            alert = AlertContent(title: "音声認識を再送できません", message: message)
            return false
        }
        configurationSaveGeneration &+= 1
        beginJobRequest()
        defer { endJobRequest() }
        do {
            guard let response = try await transcriptionRetry.confirm(id: confirmation.id) else { return false }
            track(jobID: response.jobID)
            showToast("確認した結果不明区間の再送ジョブを開始しました (\(response.jobID))")
            if selectedMeetingID == confirmation.meetingID { await loadDetail() }
            return true
        } catch {
            report(error, title: "音声認識を再送できません")
            return false
        }
    }

    func confirmTranscriptionRequestRecovery(_ confirmation: TranscriptionRequestRecoveryConfirmation) async -> Bool {
        guard supportsTranscriptionModels, desktopSession.serverReachable,
            transcriptionModelCatalog.supportedProviders.contains("openai-api"),
            selectedMeetingID == confirmation.request.meetingID else {
            transcriptionRequestRecovery.invalidate()
            return false
        }
        beginJobRequest()
        defer {
            endJobRequest()
            // A late receipt resolves only its original warning, without changing selection.
            for receipt in transcriptionRequestRecovery.resolvedReceipts {
                transcriptionRetry.acknowledgeResolvedRequest(
                    requestID: receipt.requestID, meetingID: receipt.response.meetingID)
            }
        }
        do {
            guard let response = try await transcriptionRequestRecovery.confirm(id: confirmation.id) else { return false }
            track(jobID: response.jobID)
            showToast("元の再送要求の受付を確認しました (\(response.jobID))")
            if selectedMeetingID == response.meetingID { await loadDetail() }
            return true
        } catch {
            report(error, title: "元の要求の受付を確認できません")
            return false
        }
    }
}
