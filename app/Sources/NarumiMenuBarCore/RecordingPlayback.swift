import Foundation

/// The server resolves and validates this local artifact before returning it from get_meeting.
public struct RecordingPlayback: Codable, Equatable, Sendable {
    public var path: String
    public var sha256: String
    public var bytes: Int

    public init(path: String, sha256: String, bytes: Int) {
        self.path = path
        self.sha256 = sha256
        self.bytes = bytes
    }
}

public struct PrepareRecordingResponse: Codable, Equatable, Sendable {
    public var meetingID: String
    public var jobID: String

    enum CodingKeys: String, CodingKey {
        case meetingID = "meeting_id"
        case jobID = "job_id"
    }
}

/// Playback availability is independent of transcription or minutes generation.
public struct RecordingPlaybackPresentation: Equatable, Sendable {
    public let playbackPath: String?
    public let canPrepare: Bool
    public let preparationLabel: String
    public let message: String
    public let recorderWarning: String?

    public init(detail: MeetingDetail, playbackFileExists: Bool) {
        let recording = detail.recording
        let hasData = recording.tracks.values.contains { !$0.discarded && ($0.bytes ?? 0) > 0 }
        if recording.recorderError != nil {
            recorderWarning = hasData
                ? "録画が途中で終了しました。停止までに保存できたデータは残っています。"
                : "録画が途中で終了しました。録画データを保存できていない可能性があります。"
        } else {
            recorderWarning = nil
        }
        preparationLabel = detail.playback == nil ? "録画ファイルを生成" : "録画ファイルを再生成"
        if detail.meeting.status == "recording" {
            playbackPath = nil
            canPrepare = false
            message = "録画の停止後に、画面と音声をまとめた録画ファイルを生成します。"
            return
        }
        if let playback = detail.playback, playback.path.hasPrefix("/"), playback.bytes > 0,
            playbackFileExists {
            playbackPath = playback.path
        } else {
            playbackPath = nil
        }
        let unavailable = Self.preparationUnavailable(recording)
        canPrepare = unavailable == nil
        if let unavailable {
            message = unavailable
        } else if playbackPath != nil {
            message = "画面と音声をまとめた録画です。"
        } else {
            message = detail.playback == nil
                ? "画面とマイク・システム音声をまとめた録画ファイルを、この Mac 上で生成します。"
                : "録画ファイルが見つかりません。保存済みの元データから作り直せます。"
        }
    }

    private static func preparationUnavailable(_ recording: MeetingRecordingInfo) -> String? {
        guard let screen = recording.tracks["screen"] else {
            return "元の画面録画がないため、録画ファイルを生成できません。"
        }
        guard !screen.discarded else {
            return "元の画面録画が破棄されているため、録画ファイルを生成できません。"
        }
        guard screen.bytes != 0 else {
            return "元の画面録画が空のため、録画ファイルを生成できません。"
        }
        guard ["mic", "system"].contains(where: { name in
            guard let track = recording.tracks[name] else { return false }
            return !track.discarded && track.bytes != 0
        }) else {
            return "元の音声が残っていないため、録画ファイルを生成できません。"
        }
        return nil
    }

    /// The first status query can already be terminal for a short recording.
    public static func shouldRefreshDetail(after job: Job, previous: Job?) -> Bool {
        guard ["succeeded", "failed", "cancelled"].contains(job.status) else { return false }
        return previous == nil || previous?.isActive == true
    }

    /// The recording becomes playable while later transcription and minutes work continues.
    public static func shouldRefreshPlayback(after job: Job, previous: Job?) -> Bool {
        guard job.kind == "process", job.status == "running", let stage = job.progress?.stage else { return false }
        if stage == "recording/playback" {
            return job.progress?.fraction == 1 && previous?.progress?.fraction != 1
        }
        return previous?.progress?.stage == nil || previous?.progress?.stage == "recording/playback"
    }
}
