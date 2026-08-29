import Foundation
@testable import NarumiMenuBarCore

enum TranscriptionRetryFixtures {
    static let meetingID = "20260829T000000Z-a1b2c3d4"
    static let jobID = "job-0123456789ab"
    static let connectionID = "conn-0123456789ab"
    static let timestamp = "2026-08-29T00:00:00Z"
    static let inputFingerprint = String(repeating: "a", count: 64)
    static let chunkFingerprint = String(repeating: "b", count: 64)

    static func detailsObject() -> [String: Any] {
        [
            "stage": "transcribe", "reason": "transcription_outcome_unknown", "outcome_unknown": true,
            "input_fingerprint": inputFingerprint, "chunk_fingerprint": chunkFingerprint,
            "blocked_epoch": 0, "track": "system", "chunk_index": 1, "chunk_count": 3,
            "completed_chunks": 1, "start_sample": 0, "end_sample": 9_600_000, "sample_rate": 16_000,
        ]
    }

    static func details(_ updates: [String: Any] = [:]) throws -> TranscriptionOutcomeUnknownDetails {
        var value = detailsObject()
        value.merge(updates) { _, new in new }
        return try JSONDecoder().decode(
            TranscriptionOutcomeUnknownDetails.self, from: JSONSerialization.data(withJSONObject: value))
    }

    static func config(epoch: Int = 0) throws -> MeetingConfig {
        var config = MeetingConfig(
            transcriptionEngine: "fake", diarizationEngine: "none", llmProvider: "none",
            externalSendPolicy: "api_ok", language: "ja", selfName: "Fixture speaker", vocabHints: ["fixture"],
            minutesModel: MinutesModelSelection(
                provider: "openai-api", connectionID: connectionID, connectionRevision: 3,
                modelID: "fixture-text-model", maxTokens: 2048, cacheEpoch: 4))
        let selection: [String: Any] = [
            "provider": "openai-api", "connection_id": connectionID, "connection_revision": 3,
            "model_id": "whisper-1", "parameters": [String: String](), "cache_epoch": epoch,
        ]
        config.transcriptionModel = try JSONDecoder().decode(
            TranscriptionModelSelection.self, from: JSONSerialization.data(withJSONObject: selection))
        return config
    }

    static func meeting(config: MeetingConfig? = nil) throws -> MeetingDetail {
        MeetingDetail(
            meeting: MeetingSummary(
                meetingID: meetingID, meetingName: "Fixture meeting", scope: "fixture-scope",
                status: "failed", startedAt: timestamp),
            bundlePath: "/fixture/meeting", config: try config ?? self.config(),
            recording: MeetingRecordingInfo(
                startedAt: timestamp, stoppedAt: timestamp, durationSec: 1800,
                tracks: [
                    "mic": TrackStatus(
                        path: "tracks/mic.wav", sha256: String(repeating: "c", count: 64),
                        bytes: 19_200_044, durationSec: 600, discarded: false),
                    "system": TrackStatus(
                        path: "tracks/system.wav", sha256: String(repeating: "d", count: 64),
                        bytes: 38_400_044, durationSec: 1200, discarded: false),
                ]),
            contexts: [], minutesVersions: [], latestMinutes: nil, exports: [], artifacts: [])
    }

    static func job(details: TranscriptionOutcomeUnknownDetails? = nil) -> Job {
        Job(
            jobID: jobID, meetingID: meetingID, kind: "process", status: "failed", progress: nil, result: nil,
            error: ToolErrorInfo(code: "engine_unavailable", message: "Fixture failure", transcriptionOutcome: details),
            createdAt: timestamp, updatedAt: timestamp)
    }
}

actor FakeTranscriptionRetryClient: TranscriptionRetryClient {
    enum Operation: CaseIterable, Hashable, Sendable { case meeting, job, save, regenerate }
    enum RemoteFailure: Error { case fixturePrivateUpstreamBody }
    struct Save: Equatable, Sendable {
        let meetingID: String
        let scope: String?
        let expectedConfig: MeetingConfig
        let selection: TranscriptionModelSelection
        let requestID: String
    }
    struct Generation: Equatable, Sendable {
        let meetingID: String
        let scope: String?
        let expectedConfig: MeetingConfig
        let retry: TranscriptionRetry
        let requestID: String
    }

    private(set) var currentMeeting: MeetingDetail
    private(set) var currentJob: Job
    private(set) var events: [Operation] = []
    private(set) var saves: [Save] = []
    private(set) var generations: [Generation] = []
    private(set) var acceptedRetries: [TranscriptionRetry] = []
    private var actualRetry: TranscriptionRetry?
    private var hold: Operation?
    private var paused: CheckedContinuation<Void, Never>?
    private var waiters: [Operation: [CheckedContinuation<Void, Never>]] = [:]
    private var failureAt: Operation?
    private var saveResponse: SetMeetingConfigResponse?
    private var generationResponse: RegenerateResponse?

    init(meeting: MeetingDetail, job: Job, hold: Operation? = nil) {
        currentMeeting = meeting
        currentJob = job
        actualRetry = job.error?.transcriptionOutcome?.retry
        self.hold = hold
    }

    func replaceMeeting(_ value: MeetingDetail) { currentMeeting = value }
    func replaceJob(_ value: Job) { currentJob = value }
    func replaceActualRetry(_ value: TranscriptionRetry) { actualRetry = value }
    func fail(at operation: Operation) { failureAt = operation }
    func returnSave(_ value: SetMeetingConfigResponse) { saveResponse = value }
    func returnGeneration(_ value: RegenerateResponse) { generationResponse = value }

    func waitFor(_ operation: Operation) async {
        if events.contains(operation) { return }
        await withCheckedContinuation { waiters[operation, default: []].append($0) }
    }

    func release() {
        hold = nil
        paused?.resume()
        paused = nil
    }

    func meeting(id: String, scope: String?) async throws -> MeetingDetail {
        let result = currentMeeting
        await record(.meeting)
        if failureAt == .meeting { throw RemoteFailure.fixturePrivateUpstreamBody }
        return result
    }

    func jobStatus(jobID: String) async throws -> Job {
        let result = currentJob
        await record(.job)
        if failureAt == .job { throw RemoteFailure.fixturePrivateUpstreamBody }
        return result
    }

    func saveRetryEpoch(
        meetingID: String, scope: String?, expectedConfig: MeetingConfig,
        selection: TranscriptionModelSelection, requestID: String
    ) async throws -> SetMeetingConfigResponse {
        saves.append(Save(
            meetingID: meetingID, scope: scope, expectedConfig: expectedConfig,
            selection: selection, requestID: requestID))
        let matches = currentMeeting.meeting.meetingID == meetingID
            && currentMeeting.meeting.scope == scope && currentMeeting.config == expectedConfig
        if matches { currentMeeting.config.transcriptionModel = selection }
        let result = saveResponse ?? SetMeetingConfigResponse(
            meetingID: currentMeeting.meeting.meetingID, config: currentMeeting.config, scope: currentMeeting.meeting.scope)
        await record(.save)
        guard matches, failureAt != .save else { throw RemoteFailure.fixturePrivateUpstreamBody }
        return result
    }

    func regenerateRetry(
        meetingID: String, scope: String?, expectedConfig: MeetingConfig,
        retry: TranscriptionRetry, requestID: String
    ) async throws -> RegenerateResponse {
        generations.append(Generation(
            meetingID: meetingID, scope: scope, expectedConfig: expectedConfig, retry: retry, requestID: requestID))
        let matches = currentMeeting.meeting.meetingID == meetingID && currentMeeting.meeting.scope == scope
            && currentMeeting.config == expectedConfig && retry == actualRetry
            && (expectedConfig.transcriptionModel?.cacheEpoch ?? -1) > retry.blockedEpoch
        if matches, let epoch = expectedConfig.transcriptionModel?.cacheEpoch {
            acceptedRetries.append(retry)
            actualRetry = try TranscriptionRetry(
                inputFingerprint: retry.inputFingerprint, chunkFingerprint: retry.chunkFingerprint,
                blockedEpoch: epoch)
        }
        let result = generationResponse ?? RegenerateResponse(jobID: "job-111122223333", meetingID: meetingID)
        await record(.regenerate)
        guard matches, failureAt != .regenerate else { throw RemoteFailure.fixturePrivateUpstreamBody }
        return result
    }

    private func record(_ operation: Operation) async {
        events.append(operation)
        for waiter in waiters.removeValue(forKey: operation) ?? [] { waiter.resume() }
        if hold == operation { await withCheckedContinuation { paused = $0 } }
    }
}
