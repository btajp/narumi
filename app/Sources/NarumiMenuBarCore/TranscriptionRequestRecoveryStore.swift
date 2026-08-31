import Foundation
import Observation

/// Pending records come only from the desktop client's RAM. Recovery is one public MCP POST.
public protocol TranscriptionRequestRecoveryClient: Sendable {
    func pendingTranscriptionRequests() async -> [TranscriptionRequestRecovery]
    func recoverTranscriptionRequest(_ request: TranscriptionRequestRecovery) async throws -> RegenerateResponse
}

public struct TranscriptionRequestRecoveryConfirmation: Equatable, Sendable, Identifiable {
    public let id: UUID
    public let request: TranscriptionRequestRecovery

    fileprivate init(request: TranscriptionRequestRecovery) {
        id = UUID()
        self.request = request
    }
}

/// A validated receipt stays correlated with its original request even after the view changes.
public struct TranscriptionResolvedReceipt: Equatable, Sendable, Identifiable {
    public let requestID: String
    public let response: RegenerateResponse
    public var id: String { requestID }
}

@MainActor
@Observable
public final class TranscriptionRequestRecoveryStore {
    public enum State: Equatable, Sendable {
        case idle, loading, awaitingConfirmation, checking, submitting, started, cancelled, requiresReview
    }

    public private(set) var requests: [TranscriptionRequestRecovery] = []
    public private(set) var confirmation: TranscriptionRequestRecoveryConfirmation?
    public private(set) var state: State = .idle
    public private(set) var failure: TranscriptionRequestRecoveryFailure?
    public private(set) var startedJob: RegenerateResponse?
    public private(set) var unresolvedRequests: [TranscriptionRequestRecovery] = []
    public private(set) var resolvedReceipts: [TranscriptionResolvedReceipt] = []
    @ObservationIgnored private let client: any TranscriptionRequestRecoveryClient
    @ObservationIgnored private var generation: UInt64 = 0
    @ObservationIgnored private var failureRequestID: String?

    public init(client: any TranscriptionRequestRecoveryClient) { self.client = client }

    public var isBusy: Bool { [.loading, .checking, .submitting].contains(state) }
    public var canConfirm: Bool { state == .awaitingConfirmation && confirmation != nil }

    public var feedback: String? {
        var messages = unresolvedRequests.map {
            "会議 \($0.meetingID)・要求 \($0.requestID): \(TranscriptionRequestRecoveryFailure.outcomeUnknown.errorDescription ?? "受付結果が不明です。")"
        }
        if let failure {
            if !unresolvedRequests.contains(where: { $0.requestID == failureRequestID }),
                let message = failure.errorDescription { messages.append(message) }
        } else if let message = stateFeedback { messages.append(message) }
        return messages.isEmpty ? nil : messages.joined(separator: "\n")
    }

    /// Observer publications while a confirmation is executing must not cancel that operation.
    /// This method only reads RAM; it never resends a request or resolves an uncertain receipt.
    public func reload() async {
        guard !isBusy else { return }
        generation &+= 1
        let token = generation
        let previousState = state
        state = .loading
        let pending = await client.pendingTranscriptionRequests()
        guard mayContinue(token) else { return }
        requests = Self.unambiguous(pending)
        if let confirmation {
            if requests.contains(confirmation.request) {
                state = .awaitingConfirmation
            } else {
                self.confirmation = nil
                failure = .requestChanged
                failureRequestID = nil
                state = .requiresReview
            }
        } else {
            state = previousState == .loading ? .idle : previousState
        }
    }

    /// Creates a local confirmation token only. The original wire request remains immutable.
    @discardableResult
    public func prepare(_ request: TranscriptionRequestRecovery) -> TranscriptionRequestRecoveryConfirmation? {
        guard !isBusy else { return nil }
        generation &+= 1
        startedJob = nil
        failureRequestID = nil
        guard requests.contains(request) else {
            confirmation = nil
            failure = .requestChanged
            state = .requiresReview
            return nil
        }
        let value = TranscriptionRequestRecoveryConfirmation(request: request)
        confirmation = value
        failure = nil
        state = .awaitingConfirmation
        return value
    }

    /// The confirmation is consumed before awaiting. No CAS, new epoch, proof, or request ID is created.
    public func confirm(id: UUID) async throws -> RegenerateResponse? {
        guard canConfirm, let confirmed = confirmation, confirmed.id == id else { return nil }
        guard !Task.isCancelled else { cancel(); return nil }
        generation &+= 1
        let token = generation
        state = .checking
        failure = nil
        failureRequestID = nil
        let pending = await client.pendingTranscriptionRequests()
        guard mayContinue(token) else { return nil }
        requests = Self.unambiguous(pending)
        guard requests.contains(confirmed.request) else {
            confirmation = nil
            failure = .requestChanged
            state = .requiresReview
            throw TranscriptionRequestRecoveryFailure.requestChanged
        }
        state = .submitting
        do {
            let result = try await client.recoverTranscriptionRequest(confirmed.request)
            let continuing = mayContinue(token)
            let validReceipt = result.meetingID == confirmed.request.meetingID
                && result.jobID.range(of: #"\Ajob-[0-9a-f]{12,32}\z"#, options: .regularExpression) != nil
            if validReceipt { recordResolved(confirmed.request, response: result) }
            guard continuing else { return nil }
            guard validReceipt else {
                throw TranscriptionRequestRecoveryFailure.outcomeUnknown
            }
            confirmation = nil
            startedJob = result
            state = .started
            return result
        } catch {
            guard mayContinue(token) else { return nil }
            rememberUncertainty(confirmed.request)
            confirmation = nil
            failure = .outcomeUnknown
            failureRequestID = confirmed.request.requestID
            state = .requiresReview
            throw TranscriptionRequestRecoveryFailure.outcomeUnknown
        }
    }

    /// Stops the local continuation. The server may already have accepted an in-flight POST.
    public func cancel() {
        generation &+= 1
        if state == .submitting, let confirmed = confirmation {
            rememberUncertainty(confirmed.request)
            failure = .outcomeUnknown
            failureRequestID = confirmed.request.requestID
            state = .requiresReview
        } else if state != .started && state != .requiresReview {
            failure = nil
            failureRequestID = nil
            state = .cancelled
        }
        confirmation = nil
    }

    /// Server, capability, session, meeting, and window changes invalidate outstanding confirmations.
    public func invalidate() {
        cancel()
        requests = []
        startedJob = nil
        state = failure == nil ? .idle : .requiresReview
    }

    private var stateFeedback: String? {
        switch state {
        case .loading: return "アプリが保持している未確定の要求を確認しています。外部送信は行いません。"
        case .checking: return "元の要求 ID と本文が現在も一致するか確認しています。"
        case .submitting:
            return "同じ要求を1回再送して受付を確認しています。未受付だった場合は処理や API 課金が始まる可能性があります。"
        case .started: return "この要求の受付結果を確認しました。処理の結果はジョブの状態を確認してください。"
        case .cancelled: return "受付確認を取り消しました。この操作では要求を再送していません。"
        default: return nil
        }
    }

    private func mayContinue(_ token: UInt64) -> Bool {
        guard token == generation else { return false }
        guard !Task.isCancelled else { cancel(); return false }
        return true
    }

    private func rememberUncertainty(_ request: TranscriptionRequestRecovery) {
        if !unresolvedRequests.contains(request) { unresolvedRequests.append(request) }
    }

    private func recordResolved(_ request: TranscriptionRequestRecovery, response: RegenerateResponse) {
        let receipt = TranscriptionResolvedReceipt(requestID: request.requestID, response: response)
        if !resolvedReceipts.contains(where: {
            $0.requestID.utf8.elementsEqual(request.requestID.utf8) && $0.response == response
        }) { resolvedReceipts.append(receipt) }
        requests.removeAll { $0 == request }
        unresolvedRequests.removeAll { $0 == request }
        if failureRequestID?.utf8.elementsEqual(request.requestID.utf8) == true, failure == .outcomeUnknown {
            failure = nil
            failureRequestID = nil
        }
    }

    private static func unambiguous(_ values: [TranscriptionRequestRecovery]) -> [TranscriptionRequestRecovery] {
        let counts = Dictionary(grouping: values, by: \.requestID).mapValues { $0.count }
        return values.filter { counts[$0.requestID] == 1 }
    }
}

public enum TranscriptionRequestRecoveryFailure: Error, Equatable, LocalizedError, Sendable {
    case requestChanged, outcomeUnknown

    public var errorDescription: String? {
        switch self {
        case .requestChanged:
            return "保持している元の要求が変わったため再送せず停止しました。一覧を確認してください。"
        case .outcomeUnknown:
            return "受付結果を確認できません。自動再送しません。処理や API 課金が始まっている可能性があるため、ジョブの状態を確認してください。"
        }
    }
}
