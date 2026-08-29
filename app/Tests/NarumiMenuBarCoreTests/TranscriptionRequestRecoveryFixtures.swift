import Foundation
@testable import NarumiMenuBarCore

enum TranscriptionRequestRecoveryFixtures {
    static func request(
        requestID: String = "asr-recovery-fixture-001", changes: [String: Any] = [:], trailingWhitespace: Bool = false
    ) throws -> TranscriptionRequestRecovery {
        let config = try TranscriptionRetryFixtures.config(epoch: 1)
        let retry = try TranscriptionRetryFixtures.details().retry
        var arguments: [String: Any] = [
            "meeting_id": TranscriptionRetryFixtures.meetingID, "scope": "fixture-scope", "request_id": requestID,
            "force": false, "reason": "Fixture explicit retry",
            "expected_config": try JSONSerialization.jsonObject(with: JSONEncoder().encode(config)),
            "transcription_retry": try JSONSerialization.jsonObject(with: JSONEncoder().encode(retry)),
        ]
        arguments.merge(changes) { _, new in new }
        var data = try JSONSerialization.data(withJSONObject: arguments, options: [.prettyPrinted, .sortedKeys])
        if trailingWhitespace { data.append(contentsOf: [10, 32, 10]) }
        return try TranscriptionRequestRecovery(request: DesktopJobRequestState.Request(
            requestID: requestID, tool: ToolCatalog.regenerate, arguments: data))
    }
}

actor FakeTranscriptionRequestRecoveryClient: TranscriptionRequestRecoveryClient {
    enum RemoteFailure: Error { case fixturePrivateTransportDetails }
    private(set) var pending: [TranscriptionRequestRecovery]
    private(set) var readCount = 0
    private(set) var recoveryCalls: [TranscriptionRequestRecovery] = []
    private(set) var postedRequests: [TranscriptionRequestRecovery] = []
    private var inFlightIDs: Set<String> = []
    private var heldRead: Int?
    private var holdPost: Bool
    private var paused: CheckedContinuation<Void, Never>?
    private var readWaiters: [Int: [CheckedContinuation<Void, Never>]] = [:]
    private var postWaiters: [CheckedContinuation<Void, Never>] = []
    private var fails = false
    private var receipt: RegenerateResponse?

    init(pending: [TranscriptionRequestRecovery], heldRead: Int? = nil, holdPost: Bool = false) {
        self.pending = pending
        self.heldRead = heldRead
        self.holdPost = holdPost
    }

    func replacePending(_ values: [TranscriptionRequestRecovery]) { pending = values }
    func fail(_ value: Bool = true) { fails = value }
    func returnReceipt(_ value: RegenerateResponse) { receipt = value }

    func pendingTranscriptionRequests() async -> [TranscriptionRequestRecovery] {
        let snapshot = pending
        readCount += 1
        for waiter in readWaiters.removeValue(forKey: readCount) ?? [] { waiter.resume() }
        if heldRead == readCount { await withCheckedContinuation { paused = $0 } }
        return snapshot
    }

    func recoverTranscriptionRequest(_ request: TranscriptionRequestRecovery) async throws -> RegenerateResponse {
        recoveryCalls.append(request)
        guard pending.contains(request), !inFlightIDs.contains(request.requestID) else {
            throw RemoteFailure.fixturePrivateTransportDetails
        }
        inFlightIDs.insert(request.requestID)
        defer { inFlightIDs.remove(request.requestID) }
        postedRequests.append(request)
        for waiter in postWaiters { waiter.resume() }
        postWaiters = []
        let response = receipt ?? RegenerateResponse(jobID: "job-111122223333", meetingID: request.meetingID)
        if holdPost { await withCheckedContinuation { paused = $0 } }
        guard !fails else { throw RemoteFailure.fixturePrivateTransportDetails }
        if response.meetingID == request.meetingID,
            response.jobID.range(of: #"\Ajob-[0-9a-f]{12,32}\z"#, options: .regularExpression) != nil {
            pending.removeAll { $0 == request }
        }
        return response
    }

    func waitForRead(_ number: Int) async {
        if readCount >= number { return }
        await withCheckedContinuation { readWaiters[number, default: []].append($0) }
    }

    func waitForPost() async {
        if !postedRequests.isEmpty { return }
        await withCheckedContinuation { postWaiters.append($0) }
    }

    func release() {
        heldRead = nil
        holdPost = false
        paused?.resume()
        paused = nil
    }
}
