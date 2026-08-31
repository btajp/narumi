import Foundation
import Observation

/// Adapters map these calls to public get_meeting/get_job_status/set_meeting_config/regenerate.
/// Saving must use expected_config under the server's lock; it is not a read-then-write substitute.
public protocol TranscriptionRetryClient: Sendable {
    func meeting(id: String, scope: String?) async throws -> MeetingDetail
    func jobStatus(jobID: String) async throws -> Job
    func saveRetryEpoch(
        meetingID: String, scope: String?, expectedConfig: MeetingConfig,
        selection: TranscriptionModelSelection, requestID: String
    ) async throws -> SetMeetingConfigResponse
    func regenerateRetry(
        meetingID: String, scope: String?, expectedConfig: MeetingConfig,
        retry: TranscriptionRetry, requestID: String
    ) async throws -> RegenerateResponse
}

public struct TranscriptionRetryConfirmation: Equatable, Sendable, Identifiable {
    public let id: UUID
    public let meetingID: String
    public let scope: String?
    public let jobID: String
    public let config: MeetingConfig
    public let recording: MeetingRecordingInfo
    public let details: TranscriptionOutcomeUnknownDetails
    public let retry: TranscriptionRetry
    public let updatedConfig: MeetingConfig

    fileprivate init(meeting: MeetingDetail, job: Job, details: TranscriptionOutcomeUnknownDetails) throws {
        guard var selection = meeting.config.transcriptionModel, selection.isWellFormed else {
            throw TranscriptionRetryFailure.invalidConfiguration
        }
        let previousEpoch = max(selection.cacheEpoch, details.blockedEpoch)
        guard previousEpoch < Int.max else { throw TranscriptionRetryFailure.epochExhausted }
        selection.cacheEpoch = previousEpoch + 1
        var nextConfig = meeting.config
        nextConfig.transcriptionModel = selection
        id = UUID()
        meetingID = meeting.meeting.meetingID
        scope = meeting.meeting.scope
        jobID = job.jobID
        config = meeting.config
        recording = meeting.recording
        self.details = details
        retry = details.retry
        updatedConfig = nextConfig
    }
}

/// An uncertain write survives view changes and later confirmations until its own receipt is recovered.
public struct TranscriptionRetryPendingOperation: Equatable, Sendable, Identifiable {
    public let requestID: String
    public let meetingID: String
    public let sourceJobID: String
    public let failure: TranscriptionRetryFailure
    public var id: String { requestID }
    public var feedback: String {
        "会議 \(meetingID)（確認元 \(sourceJobID)）: \(failure.errorDescription ?? "結果を確認できません。")"
    }
}

@MainActor
@Observable
public final class TranscriptionRetryStore {
    public enum State: Equatable, Sendable {
        case idle, awaitingConfirmation, checking, saving, submitting, started, cancelled, requiresReview
    }

    public private(set) var state: State = .idle
    public private(set) var confirmation: TranscriptionRetryConfirmation?
    public private(set) var failure: TranscriptionRetryFailure?
    public private(set) var startedJob: RegenerateResponse?
    public private(set) var unresolvedOperations: [TranscriptionRetryPendingOperation] = []
    @ObservationIgnored private let client: any TranscriptionRetryClient
    @ObservationIgnored private var generation: UInt64 = 0
    @ObservationIgnored private var currentRequestID: String?
    @ObservationIgnored private var failureRequestID: String?

    public init(client: any TranscriptionRetryClient) { self.client = client }

    public var isBusy: Bool { [.checking, .saving, .submitting].contains(state) }
    public var canConfirm: Bool { state == .awaitingConfirmation && confirmation != nil }
    public var feedback: String? {
        var messages = unresolvedOperations.map(\.feedback)
        if let failure {
            if !unresolvedOperations.contains(where: { $0.requestID == failureRequestID }),
                let message = failure.errorDescription { messages.append(message) }
        } else if let message = stateFeedback { messages.append(message) }
        return messages.isEmpty ? nil : messages.joined(separator: "\n")
    }

    private var stateFeedback: String? {
        switch state {
        case .checking: return "確認した会議設定と不明区間が現在も一致するか確認しています。"
        case .saving: return "確認した設定との一致を条件に試行番号を保存しています。再生成はまだ開始していません。"
        case .submitting:
            return "再試行の受付を確認しています。取消しても、サービス側の処理や API 課金の停止は保証できません。"
        case .started: return "再試行ジョブが受け付けられました。処理結果はジョブの状態を確認してください。"
        case .cancelled: return "再試行の確認を取り消しました。この操作では再生成を開始していません。"
        default: return nil
        }
    }

    /// Call to display a confirmation. This does not read, save, increment the stored epoch, or send audio.
    @discardableResult
    public func prepare(meeting: MeetingDetail, job: Job) throws -> TranscriptionRetryConfirmation {
        guard !isBusy else { throw TranscriptionRetryFailure.operationInProgress }
        do {
            let details = try Self.validatedDetails(meeting: meeting, job: job)
            let value = try TranscriptionRetryConfirmation(meeting: meeting, job: job, details: details)
            generation &+= 1
            confirmation = value
            startedJob = nil
            failure = nil
            currentRequestID = nil
            failureRequestID = nil
            state = .awaitingConfirmation
            return value
        } catch {
            generation &+= 1
            confirmation = nil
            startedJob = nil
            let safe = error as? TranscriptionRetryFailure ?? .invalidEvidence
            failure = safe
            currentRequestID = nil
            failureRequestID = nil
            state = .requiresReview
            throw safe
        }
    }

    /// Only an explicit user confirmation reaches this method. An id is consumed before the first await.
    /// Late replies and cancellation return nil; no failed write is automatically replayed.
    public func confirm(id: UUID) async throws -> RegenerateResponse? {
        guard canConfirm, let confirmed = confirmation, confirmed.id == id else { return nil }
        guard !Task.isCancelled else { cancel(); return nil }
        generation &+= 1
        let token = generation
        state = .checking
        failure = nil
        currentRequestID = nil
        failureRequestID = nil
        do {
            let meeting = try await client.meeting(id: confirmed.meetingID, scope: confirmed.scope)
            guard mayContinue(token) else { return nil }
            guard meeting.meeting.meetingID == confirmed.meetingID, meeting.meeting.scope == confirmed.scope,
                meeting.config == confirmed.config else { throw TranscriptionRetryFailure.configurationChanged }
            guard meeting.recording == confirmed.recording else { throw TranscriptionRetryFailure.evidenceChanged }
            let job = try await client.jobStatus(jobID: confirmed.jobID)
            guard mayContinue(token) else { return nil }
            guard job.jobID == confirmed.jobID else { throw TranscriptionRetryFailure.evidenceChanged }
            let details = try Self.validatedDetails(meeting: meeting, job: job)
            guard details == confirmed.details else { throw TranscriptionRetryFailure.evidenceChanged }
            guard let selection = confirmed.updatedConfig.transcriptionModel,
                selection.cacheEpoch > details.blockedEpoch else {
                throw TranscriptionRetryFailure.invalidConfiguration
            }

            state = .saving
            let saveRequestID = UUID().uuidString
            currentRequestID = saveRequestID
            let saved = try await client.saveRetryEpoch(
                meetingID: confirmed.meetingID, scope: confirmed.scope, expectedConfig: confirmed.config,
                selection: selection, requestID: saveRequestID)
            guard mayContinue(token) else { return nil }
            guard saved.meetingID == confirmed.meetingID, saved.scope == confirmed.scope,
                saved.config == confirmed.updatedConfig else { throw TranscriptionRetryFailure.saveResponseMismatch }

            state = .submitting
            let generationRequestID = UUID().uuidString
            currentRequestID = generationRequestID
            let result = try await client.regenerateRetry(
                meetingID: confirmed.meetingID, scope: confirmed.scope, expectedConfig: saved.config,
                retry: confirmed.retry, requestID: generationRequestID)
            let continuing = mayContinue(token)
            let validReceipt = result.meetingID == confirmed.meetingID && Self.validJobID(result.jobID)
            guard continuing else {
                if validReceipt {
                    resolveGenerationWarning(
                        requestID: generationRequestID, meetingID: confirmed.meetingID, preservingState: true)
                }
                return nil
            }
            guard validReceipt else {
                throw TranscriptionRetryFailure.generationOutcomeUnknown
            }
            startedJob = result
            state = .started
            return result
        } catch {
            guard mayContinue(token) else { return nil }
            let safe = error as? TranscriptionRetryFailure ?? failureForCurrentStage()
            rememberUncertainty(safe)
            failure = safe
            failureRequestID = currentRequestID
            state = .requiresReview
            throw safe
        }
    }

    /// Stops this continuation only. An already dispatched save or generation may still complete.
    public func cancel() {
        generation &+= 1
        if state == .saving || state == .submitting {
            let issue = failureForCurrentStage()
            rememberUncertainty(issue)
            failure = issue
            failureRequestID = currentRequestID
            state = .requiresReview
        } else if state != .started && state != .requiresReview {
            failure = nil
            failureRequestID = nil
            state = .cancelled
        }
        confirmation = nil
        currentRequestID = nil
    }

    /// The owning view calls this when its meeting/server context changes or it closes.
    public func invalidate() {
        cancel()
        startedJob = nil
        state = failure == nil ? .idle : .requiresReview
    }

    /// Only a validated receipt for this same regeneration request resolves its warning.
    /// A recovered generation receipt says nothing about a separate uncertain configuration save.
    public func acknowledgeResolvedRequest(requestID: String, meetingID: String? = nil) {
        resolveGenerationWarning(requestID: requestID, meetingID: meetingID, preservingState: false)
    }

    private func resolveGenerationWarning(requestID: String, meetingID: String?, preservingState: Bool) {
        let matched = unresolvedOperations.contains {
            $0.requestID == requestID && $0.failure == .generationOutcomeUnknown
                && (meetingID == nil || $0.meetingID == meetingID)
        }
        guard matched else { return }
        unresolvedOperations.removeAll {
            $0.requestID == requestID && $0.failure == .generationOutcomeUnknown
                && (meetingID == nil || $0.meetingID == meetingID)
        }
        if failureRequestID == requestID, failure == .generationOutcomeUnknown {
            failure = nil
            failureRequestID = nil
            if !preservingState, state == .requiresReview { state = .idle }
        }
    }

    private func rememberUncertainty(_ issue: TranscriptionRetryFailure) {
        guard [.saveOutcomeUnknown, .saveResponseMismatch, .generationOutcomeUnknown].contains(issue),
            let confirmed = confirmation, let requestID = currentRequestID,
            !unresolvedOperations.contains(where: { $0.requestID == requestID }) else { return }
        unresolvedOperations.append(TranscriptionRetryPendingOperation(
            requestID: requestID, meetingID: confirmed.meetingID, sourceJobID: confirmed.jobID, failure: issue))
    }

    private func mayContinue(_ token: UInt64) -> Bool {
        guard token == generation else { return false }
        guard !Task.isCancelled else { cancel(); return false }
        return true
    }

    private func failureForCurrentStage() -> TranscriptionRetryFailure {
        switch state {
        case .saving: return .saveOutcomeUnknown
        case .submitting: return .generationOutcomeUnknown
        default: return .stateUnavailable
        }
    }

    private static func validatedDetails(meeting: MeetingDetail, job: Job) throws -> TranscriptionOutcomeUnknownDetails {
        guard meeting.meeting.meetingID.range(of: #"\A[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}\z"#, options: .regularExpression) != nil,
            validJobID(job.jobID), job.meetingID == meeting.meeting.meetingID,
            ["process", "regenerate"].contains(job.kind), ["failed", "cancelled"].contains(job.status),
            let details = job.error?.transcriptionOutcome else { throw TranscriptionRetryFailure.invalidEvidence }
        guard !["recording", "processing"].contains(meeting.meeting.status),
            meeting.meeting.activeJob.map({ !["queued", "running"].contains($0.status) }) ?? true else {
            throw TranscriptionRetryFailure.meetingBusy
        }
        guard let selection = meeting.config.transcriptionModel, selection.isWellFormed,
            meeting.config.externalSendPolicy == "api_ok" else {
            throw TranscriptionRetryFailure.invalidConfiguration
        }
        guard details.provider.map({ $0 == selection.provider }) ?? true,
            details.modelID.map({ $0 == selection.modelID }) ?? true,
            details.connectionID.map({ $0 == selection.connectionID }) ?? true,
            details.connectionRevision.map({ $0 == selection.connectionRevision }) ?? true else {
            throw TranscriptionRetryFailure.evidenceChanged
        }
        return details
    }

    private static func validJobID(_ value: String) -> Bool {
        value.range(of: #"\Ajob-[0-9a-f]{12,32}\z"#, options: .regularExpression) != nil
    }
}

public enum TranscriptionRetryFailure: Error, Equatable, LocalizedError, Sendable {
    case invalidEvidence, invalidConfiguration, configurationChanged, evidenceChanged, meetingBusy
    case epochExhausted, operationInProgress, stateUnavailable, saveResponseMismatch
    case saveOutcomeUnknown, generationOutcomeUnknown

    public var errorDescription: String? {
        switch self {
        case .invalidEvidence:
            return "このジョブから音声認識の不明区間を確認できません。状態を再取得してください。"
        case .invalidConfiguration:
            return "API 音声認識の設定と api_ok の許可を確認してください。"
        case .configurationChanged:
            return "確認した会議設定が変わりました。上書きせず停止しました。最新の設定を確認してください。"
        case .evidenceChanged:
            return "入力・不明区間・試行番号の確認情報が変わりました。最新のジョブを確認してください。"
        case .meetingBusy:
            return "会議を録画・処理中のため再試行できません。終了後に状態を確認してください。"
        case .epochExhausted:
            return "試行番号が上限に達しているため再試行できません。"
        case .operationInProgress:
            return "再試行の確認または保存を処理中です。完了するまで次の操作は開始しません。"
        case .stateUnavailable:
            return "現在の会議・ジョブの状態を確認できません。保存・再生成は開始していません。"
        case .saveResponseMismatch:
            return "保存結果が確認した設定と一致しないため再生成しません。会議設定を再取得してください。"
        case .saveOutcomeUnknown:
            return "設定の保存結果を確認できません。再生成は開始せず、自動再保存もしません。会議設定を再取得してください。"
        case .generationOutcomeUnknown:
            return "再生成の受付結果が不明です。自動再送しません。処理や API 課金が始まっている可能性があるため、ジョブの状態を確認してください。"
        }
    }
}
