import AppKit
import Foundation
import NarumiMenuBarCore

extension MainWindowModel {
    var playbackPresentation: RecordingPlaybackPresentation? {
        guard let detail else { return nil }
        return RecordingPlaybackPresentation(
            detail: detail, playbackFileExists: detail.playback.map { Self.isPlaybackFileAvailable($0.path) } ?? false)
    }

    var playbackJob: Job? {
        guard let meetingID = detail?.meeting.meetingID else { return nil }
        return jobs.first { $0.meetingID == meetingID && ["playback", "process"].contains($0.kind) }
    }

    var playbackBusy: Bool {
        guard let detail else { return false }
        let active: ActiveJob?
        if let listed = meetings.first(where: { $0.meetingID == detail.meeting.meetingID }) {
            active = listed.activeJob
        } else {
            active = detail.meeting.activeJob
        }
        let unresolvedActive = active.map { candidate in
            if let known = jobs.first(where: { $0.jobID == candidate.jobID }) { return known.isActive }
            return candidate.status == "queued" || candidate.status == "running"
        } ?? false
        return preparingPlaybackMeetingID == detail.meeting.meetingID || unresolvedJobRequestCount > 0
            || jobs.contains { $0.meetingID == detail.meeting.meetingID && $0.isActive }
            || unresolvedActive
    }

    func preparePlayback() async {
        guard let detail, detail.meeting.meetingID == selectedMeetingID,
            playbackPresentation?.canPrepare == true, !playbackBusy else { return }
        let meetingID = detail.meeting.meetingID
        preparingPlaybackMeetingID = meetingID
        beginJobRequest()
        defer {
            if preparingPlaybackMeetingID == meetingID { preparingPlaybackMeetingID = nil }
            endJobRequest()
        }
        do {
            var arguments: [String: JSONNode] = [
                "meeting_id": .string(meetingID), "request_id": .string(UUID().uuidString),
            ]
            if let scope = detail.meeting.scope { arguments["scope"] = .string(scope) }
            let response: PrepareRecordingResponse = try await client.call(ToolCatalog.prepareRecording, arguments)
            track(jobID: response.jobID)
            showToast("録画ファイルの生成を開始しました。")
            await refresh()
        } catch {
            report(error, title: "録画ファイルを生成できません")
        }
    }
}
