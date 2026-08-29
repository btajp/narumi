import Foundation

public struct MinutesRetry: Codable, Equatable, Sendable {
    public let runID: String
    public let nodeID: String
    public let callID: String
    public let blockedAttemptID: String

    enum CodingKeys: String, CodingKey, CaseIterable {
        case runID = "run_id"
        case nodeID = "node_id"
        case callID = "call_id"
        case blockedAttemptID = "blocked_attempt_id"
    }

    public init(runID: String, nodeID: String, callID: String, blockedAttemptID: String) throws {
        self.runID = runID
        self.nodeID = nodeID
        self.callID = callID
        self.blockedAttemptID = blockedAttemptID
        guard isWellFormed else { throw MinutesRetryFailure.invalidProof }
    }

    public init(details: MinutesOutcomeUnknownDetails) throws {
        guard details.attemptsUsed < details.attemptLimit,
            details.retryAttemptsUsed < details.retryAttemptLimit else {
            throw MinutesRetryFailure.retryLimitReached
        }
        try self.init(
            runID: details.target.runID, nodeID: details.target.nodeID,
            callID: details.target.callID, blockedAttemptID: details.blockedAttemptID)
    }

    public init(from decoder: Decoder) throws {
        try requireContractKeys(
            decoder, allowed: Set(CodingKeys.allCases.map(\.rawValue)),
            required: Set(CodingKeys.allCases.map(\.rawValue)))
        let container = try decoder.container(keyedBy: CodingKeys.self)
        runID = try container.decode(String.self, forKey: .runID)
        nodeID = try container.decode(String.self, forKey: .nodeID)
        callID = try container.decode(String.self, forKey: .callID)
        blockedAttemptID = try container.decode(String.self, forKey: .blockedAttemptID)
        guard isWellFormed else {
            throw DecodingError.dataCorruptedError(
                forKey: .runID, in: container, debugDescription: "Invalid minutes retry proof")
        }
    }

    public var isWellFormed: Bool {
        ProcessingIdentifier.run(runID) && ProcessingIdentifier.node(nodeID)
            && ProcessingIdentifier.call(callID) && ProcessingIdentifier.attempt(blockedAttemptID)
    }
}

public enum MinutesRetryFailure: Error, Equatable, LocalizedError, Sendable {
    case invalidProof, retryLimitReached

    public var errorDescription: String? {
        switch self {
        case .invalidProof: return "議事録生成の再送確認情報が不正です。"
        case .retryLimitReached: return "この不明結果は確認付き再送の上限64回に達しています。"
        }
    }
}
