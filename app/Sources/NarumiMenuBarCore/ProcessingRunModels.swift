import Foundation

public enum ProcessingRunStatus: String, Codable, CaseIterable, Sendable {
    case prepared, running, blocked, succeeded, failed, cancelled, interrupted
}

public enum ProcessingNodeStatus: String, Codable, CaseIterable, Sendable {
    case prepared, submitted, succeeded, reused, failed, unknown, cancelled
}

public enum ProcessingNodeRole: String, Codable, CaseIterable, Sendable {
    case generator, synthesizer
}

public enum ProcessingNodePhase: String, Codable, CaseIterable, Sendable {
    case chunk, reduce, final
}

public struct ProcessingTargetRef: Codable, Equatable, Sendable {
    public let runID: String
    public let nodeID: String
    public let callID: String

    enum CodingKeys: String, CodingKey, CaseIterable {
        case runID = "run_id", nodeID = "node_id", callID = "call_id"
    }

    public init(runID: String, nodeID: String, callID: String) {
        self.runID = runID; self.nodeID = nodeID; self.callID = callID
    }

    public init(from decoder: Decoder) throws {
        try Self.checkKeys(decoder)
        let c = try decoder.container(keyedBy: CodingKeys.self)
        self.init(runID: try c.decode(String.self, forKey: .runID),
                  nodeID: try c.decode(String.self, forKey: .nodeID),
                  callID: try c.decode(String.self, forKey: .callID))
        guard isWellFormed else { throw Self.corrupt(.runID, c) }
    }

    public var isWellFormed: Bool {
        ProcessingIdentifier.run(runID) && ProcessingIdentifier.node(nodeID) && ProcessingIdentifier.call(callID)
    }

    private static func checkKeys(_ decoder: Decoder) throws {
        try requireContractKeys(decoder, allowed: Set(CodingKeys.allCases.map(\.rawValue)),
                                required: Set(CodingKeys.allCases.map(\.rawValue)))
    }
    private static func corrupt(_ key: CodingKeys, _ c: KeyedDecodingContainer<CodingKeys>) -> DecodingError {
        .dataCorruptedError(forKey: key, in: c, debugDescription: "Invalid processing target")
    }
}

public struct ProcessingOriginRef: Codable, Equatable, Sendable {
    public let runID: String
    public let nodeID: String
    public let callID: String
    public let attemptID: String
    public let provider: String
    public let connectionID: String
    public let connectionRevision: Int
    public let modelID: String

    enum CodingKeys: String, CodingKey, CaseIterable {
        case runID = "run_id", nodeID = "node_id", callID = "call_id", attemptID = "attempt_id"
        case provider, connectionID = "connection_id", connectionRevision = "connection_revision", modelID = "model_id"
    }

    public init(
        runID: String, nodeID: String, callID: String, attemptID: String, provider: String,
        connectionID: String, connectionRevision: Int, modelID: String
    ) {
        self.runID = runID; self.nodeID = nodeID; self.callID = callID; self.attemptID = attemptID
        self.provider = provider; self.connectionID = connectionID
        self.connectionRevision = connectionRevision; self.modelID = modelID
    }

    public init(from decoder: Decoder) throws {
        try requireContractKeys(decoder, allowed: Set(CodingKeys.allCases.map(\.rawValue)),
                                required: Set(CodingKeys.allCases.map(\.rawValue)))
        let c = try decoder.container(keyedBy: CodingKeys.self)
        self.init(runID: try c.decode(String.self, forKey: .runID),
                  nodeID: try c.decode(String.self, forKey: .nodeID),
                  callID: try c.decode(String.self, forKey: .callID),
                  attemptID: try c.decode(String.self, forKey: .attemptID),
                  provider: try c.decode(String.self, forKey: .provider),
                  connectionID: try c.decode(String.self, forKey: .connectionID),
                  connectionRevision: try c.decode(Int.self, forKey: .connectionRevision),
                  modelID: try c.decode(String.self, forKey: .modelID))
        guard isWellFormed else {
            throw DecodingError.dataCorruptedError(forKey: .runID, in: c, debugDescription: "Invalid processing origin")
        }
    }

    public var target: ProcessingTargetRef { .init(runID: runID, nodeID: nodeID, callID: callID) }
    public var isWellFormed: Bool {
        target.isWellFormed && ProcessingIdentifier.attempt(attemptID)
            && MinutesModelSelection.providers.contains(provider)
            && connectionRevision > 0
            && connectionID.range(of: #"\Aconn-[a-f0-9]{12,32}\z"#, options: .regularExpression) != nil
            && !modelID.isEmpty && modelID.count <= 256
    }
}

public struct ProcessingCurrentSelection: Codable, Equatable, Sendable {
    public let provider: String
    public let connectionID: String
    public let connectionRevision: Int
    public let modelID: String

    enum CodingKeys: String, CodingKey, CaseIterable {
        case provider, connectionID = "connection_id", connectionRevision = "connection_revision", modelID = "model_id"
    }

    public init(provider: String, connectionID: String, connectionRevision: Int, modelID: String) {
        self.provider = provider; self.connectionID = connectionID
        self.connectionRevision = connectionRevision; self.modelID = modelID
    }

    public init(from decoder: Decoder) throws {
        try requireContractKeys(decoder, allowed: Set(CodingKeys.allCases.map(\.rawValue)),
                                required: Set(CodingKeys.allCases.map(\.rawValue)))
        let c = try decoder.container(keyedBy: CodingKeys.self)
        self.init(provider: try c.decode(String.self, forKey: .provider),
                  connectionID: try c.decode(String.self, forKey: .connectionID),
                  connectionRevision: try c.decode(Int.self, forKey: .connectionRevision),
                  modelID: try c.decode(String.self, forKey: .modelID))
        guard isWellFormed else {
            throw DecodingError.dataCorruptedError(forKey: .provider, in: c, debugDescription: "Invalid current selection")
        }
    }

    public var isWellFormed: Bool {
        MinutesModelSelection.providers.contains(provider) && connectionRevision > 0
            && connectionID.range(of: #"\Aconn-[a-f0-9]{12,32}\z"#, options: .regularExpression) != nil
            && !modelID.isEmpty && modelID.count <= 256
    }
}

public enum ProcessingRetryOutcome: String, Codable, CaseIterable, Sendable {
    case unknown
    case knownFailed = "known_failed"
    case succeeded
}

public struct ProcessingRetryAttemptSummary: Codable, Equatable, Sendable {
    public let attempt: ProcessingOriginRef
    public let outcome: ProcessingRetryOutcome

    enum CodingKeys: String, CodingKey, CaseIterable { case attempt, outcome }
    public init(attempt: ProcessingOriginRef, outcome: ProcessingRetryOutcome) {
        self.attempt = attempt; self.outcome = outcome
    }
    public init(from decoder: Decoder) throws {
        try requireContractKeys(decoder, allowed: Set(CodingKeys.allCases.map(\.rawValue)),
                                required: Set(CodingKeys.allCases.map(\.rawValue)))
        let c = try decoder.container(keyedBy: CodingKeys.self)
        self.init(attempt: try c.decode(ProcessingOriginRef.self, forKey: .attempt),
                  outcome: try c.decode(ProcessingRetryOutcome.self, forKey: .outcome))
    }
}

public struct ProcessingRetryLineage: Codable, Equatable, Sendable {
    public let originUnknown: ProcessingOriginRef
    public let attemptChain: [ProcessingRetryAttemptSummary]
    public let resolvedBy: ProcessingOriginRef?

    enum CodingKeys: String, CodingKey, CaseIterable {
        case originUnknown = "origin_unknown", attemptChain = "attempt_chain", resolvedBy = "resolved_by"
    }

    public init(
        originUnknown: ProcessingOriginRef, attemptChain: [ProcessingRetryAttemptSummary],
        resolvedBy: ProcessingOriginRef?
    ) {
        self.originUnknown = originUnknown; self.attemptChain = attemptChain; self.resolvedBy = resolvedBy
    }

    public init(from decoder: Decoder) throws {
        try requireContractKeys(decoder, allowed: Set(CodingKeys.allCases.map(\.rawValue)),
                                required: Set(CodingKeys.allCases.map(\.rawValue)))
        let c = try decoder.container(keyedBy: CodingKeys.self)
        self.init(originUnknown: try c.decode(ProcessingOriginRef.self, forKey: .originUnknown),
                  attemptChain: try c.decode([ProcessingRetryAttemptSummary].self, forKey: .attemptChain),
                  resolvedBy: try c.decodeIfPresent(ProcessingOriginRef.self, forKey: .resolvedBy))
        guard isWellFormed else {
            throw DecodingError.dataCorruptedError(forKey: .attemptChain, in: c, debugDescription: "Invalid retry lineage")
        }
    }

    public var isWellFormed: Bool {
        guard (1...64).contains(attemptChain.count), attemptChain[0].attempt == originUnknown,
              attemptChain[0].outcome == .unknown,
              Set(attemptChain.map { $0.attempt.attemptID }).count == attemptChain.count,
              !attemptChain.dropLast().contains(where: { $0.outcome == .succeeded }) else { return false }
        if let resolvedBy {
            return attemptChain.last?.outcome == .succeeded && attemptChain.last?.attempt == resolvedBy
        }
        return attemptChain.last?.outcome != .succeeded
    }
}

public struct ProcessingSafeError: Codable, Equatable, Sendable {
    public let code: String
    public let message: String
    public let reason: String?
    enum CodingKeys: String, CodingKey, CaseIterable { case code, message, reason }
    public init(code: String, message: String, reason: String?) {
        self.code = code; self.message = message; self.reason = reason
    }
    public init(from decoder: Decoder) throws {
        try requireContractKeys(decoder, allowed: Set(CodingKeys.allCases.map(\.rawValue)),
                                required: Set(CodingKeys.allCases.map(\.rawValue)))
        let c = try decoder.container(keyedBy: CodingKeys.self)
        self.init(code: try c.decode(String.self, forKey: .code),
                  message: try c.decode(String.self, forKey: .message),
                  reason: try c.decodeIfPresent(String.self, forKey: .reason))
        guard !code.isEmpty, (1...500).contains(message.count), reason.map({ (1...128).contains($0.count) }) ?? true else {
            throw DecodingError.dataCorruptedError(forKey: .message, in: c, debugDescription: "Invalid safe error")
        }
    }
}

public struct MinutesOutcomeUnknownDetails: Codable, Equatable, Sendable {
    public let stage: String
    public let reason: String
    public let outcomeUnknown: Bool
    public let target: ProcessingTargetRef
    public let blockedAttemptID: String
    public let origin: ProcessingOriginRef
    public let currentSelection: ProcessingCurrentSelection
    public let contentFingerprint: String
    public let latestAttemptOutcome: ProcessingRetryOutcome
    public let attemptsUsed: Int
    public let attemptLimit: Int
    public let retryAttemptsUsed: Int
    public let retryAttemptLimit: Int

    enum CodingKeys: String, CodingKey, CaseIterable {
        case stage, reason, outcomeUnknown = "outcome_unknown", target
        case blockedAttemptID = "blocked_attempt_id", origin, currentSelection = "current_selection"
        case contentFingerprint = "content_fingerprint", latestAttemptOutcome = "latest_attempt_outcome"
        case attemptsUsed = "attempts_used", attemptLimit = "attempt_limit"
        case retryAttemptsUsed = "retry_attempts_used", retryAttemptLimit = "retry_attempt_limit"
    }

    public init(
        target: ProcessingTargetRef, blockedAttemptID: String, origin: ProcessingOriginRef,
        currentSelection: ProcessingCurrentSelection, contentFingerprint: String,
        latestAttemptOutcome: ProcessingRetryOutcome, attemptsUsed: Int, retryAttemptsUsed: Int,
        stage: String = "generate", reason: String = "minutes_outcome_unknown",
        outcomeUnknown: Bool = true, attemptLimit: Int = 64, retryAttemptLimit: Int = 64
    ) {
        self.stage = stage; self.reason = reason; self.outcomeUnknown = outcomeUnknown
        self.target = target; self.blockedAttemptID = blockedAttemptID; self.origin = origin
        self.currentSelection = currentSelection; self.contentFingerprint = contentFingerprint
        self.latestAttemptOutcome = latestAttemptOutcome; self.attemptsUsed = attemptsUsed
        self.attemptLimit = attemptLimit; self.retryAttemptsUsed = retryAttemptsUsed
        self.retryAttemptLimit = retryAttemptLimit
    }

    public init(from decoder: Decoder) throws {
        try requireContractKeys(decoder, allowed: Set(CodingKeys.allCases.map(\.rawValue)),
                                required: Set(CodingKeys.allCases.map(\.rawValue)))
        let c = try decoder.container(keyedBy: CodingKeys.self)
        self.init(target: try c.decode(ProcessingTargetRef.self, forKey: .target),
                  blockedAttemptID: try c.decode(String.self, forKey: .blockedAttemptID),
                  origin: try c.decode(ProcessingOriginRef.self, forKey: .origin),
                  currentSelection: try c.decode(ProcessingCurrentSelection.self, forKey: .currentSelection),
                  contentFingerprint: try c.decode(String.self, forKey: .contentFingerprint),
                  latestAttemptOutcome: try c.decode(ProcessingRetryOutcome.self, forKey: .latestAttemptOutcome),
                  attemptsUsed: try c.decode(Int.self, forKey: .attemptsUsed),
                  retryAttemptsUsed: try c.decode(Int.self, forKey: .retryAttemptsUsed),
                  stage: try c.decode(String.self, forKey: .stage), reason: try c.decode(String.self, forKey: .reason),
                  outcomeUnknown: try c.decode(Bool.self, forKey: .outcomeUnknown),
                  attemptLimit: try c.decode(Int.self, forKey: .attemptLimit),
                  retryAttemptLimit: try c.decode(Int.self, forKey: .retryAttemptLimit))
        guard isWellFormed else {
            throw DecodingError.dataCorruptedError(forKey: .reason, in: c, debugDescription: "Invalid minutes unknown details")
        }
    }

    public var isWellFormed: Bool {
        stage == "generate" && reason == "minutes_outcome_unknown" && outcomeUnknown
            && target.isWellFormed && ProcessingIdentifier.attempt(blockedAttemptID)
            && origin.attemptID == blockedAttemptID && ProcessingIdentifier.sha256(contentFingerprint)
            && [.unknown, .knownFailed].contains(latestAttemptOutcome)
            && (0...64).contains(attemptsUsed) && attemptLimit == 64
            && (1...64).contains(retryAttemptsUsed) && retryAttemptLimit == 64
    }
}

public struct ProcessingNode: Codable, Equatable, Sendable, Identifiable {
    public let nodeID: String
    public let role: ProcessingNodeRole
    public let generatorID: String?
    public let slotID: String?
    public let phase: ProcessingNodePhase
    public let status: ProcessingNodeStatus
    public let callID: String?
    public let contentFingerprint: String?
    public let dependencyNodeIDs: [String]
    public let artifactID: String?
    public let origin: ProcessingOriginRef?
    public let retryLineage: ProcessingRetryLineage?
    public let reused: Bool
    public let error: ProcessingSafeError?
    public var id: String { nodeID }

    enum CodingKeys: String, CodingKey, CaseIterable {
        case nodeID = "node_id", role, generatorID = "generator_id", slotID = "slot_id", phase, status
        case callID = "call_id", contentFingerprint = "content_fingerprint"
        case dependencyNodeIDs = "dependency_node_ids", artifactID = "artifact_id", origin
        case retryLineage = "retry_lineage", reused, error
    }

    public init(from decoder: Decoder) throws {
        try requireContractKeys(decoder, allowed: Set(CodingKeys.allCases.map(\.rawValue)),
                                required: Set(CodingKeys.allCases.map(\.rawValue)))
        let c = try decoder.container(keyedBy: CodingKeys.self)
        nodeID = try c.decode(String.self, forKey: .nodeID); role = try c.decode(ProcessingNodeRole.self, forKey: .role)
        generatorID = try c.decodeIfPresent(String.self, forKey: .generatorID)
        slotID = try c.decodeIfPresent(String.self, forKey: .slotID); phase = try c.decode(ProcessingNodePhase.self, forKey: .phase)
        status = try c.decode(ProcessingNodeStatus.self, forKey: .status); callID = try c.decodeIfPresent(String.self, forKey: .callID)
        contentFingerprint = try c.decodeIfPresent(String.self, forKey: .contentFingerprint)
        dependencyNodeIDs = try c.decode([String].self, forKey: .dependencyNodeIDs)
        artifactID = try c.decodeIfPresent(String.self, forKey: .artifactID); origin = try c.decodeIfPresent(ProcessingOriginRef.self, forKey: .origin)
        retryLineage = try c.decodeIfPresent(ProcessingRetryLineage.self, forKey: .retryLineage)
        reused = try c.decode(Bool.self, forKey: .reused); error = try c.decodeIfPresent(ProcessingSafeError.self, forKey: .error)
        guard isWellFormed else {
            throw DecodingError.dataCorruptedError(forKey: .nodeID, in: c, debugDescription: "Invalid processing node")
        }
    }

    public var isWellFormed: Bool {
        guard ProcessingIdentifier.node(nodeID), dependencyNodeIDs.count <= 4096,
              Set(dependencyNodeIDs).count == dependencyNodeIDs.count,
              dependencyNodeIDs.allSatisfy(ProcessingIdentifier.node),
              callID.map(ProcessingIdentifier.call) ?? true,
              contentFingerprint.map(ProcessingIdentifier.sha256) ?? true,
              artifactID.map(ProcessingIdentifier.artifact) ?? true,
              statusFieldsAreConsistent else { return false }
        switch role {
        case .generator:
            return generatorID.map(MinutesEnsembleGenerator.isValidID) == true && slotID.map(ProcessingIdentifier.slot) == true
        case .synthesizer:
            return generatorID == nil && slotID == nil
        }
    }
}

public struct ProcessingCanonicalSlot: Codable, Equatable, Sendable, Identifiable {
    public let slotID: String
    public let generatorID: String
    public let canonicalOrdinal: Int
    public let duplicateOrdinal: Int
    public let selectionScopeSHA256: String
    public let cacheEpoch: Int
    public let draftArtifactID: String?
    public var id: String { slotID }
    enum CodingKeys: String, CodingKey, CaseIterable {
        case slotID = "slot_id", generatorID = "generator_id", canonicalOrdinal = "canonical_ordinal"
        case duplicateOrdinal = "duplicate_ordinal", selectionScopeSHA256 = "selection_scope_sha256"
        case cacheEpoch = "cache_epoch", draftArtifactID = "draft_artifact_id"
    }
    public init(from decoder: Decoder) throws {
        try requireContractKeys(decoder, allowed: Set(CodingKeys.allCases.map(\.rawValue)),
                                required: Set(CodingKeys.allCases.map(\.rawValue)))
        let c = try decoder.container(keyedBy: CodingKeys.self)
        slotID = try c.decode(String.self, forKey: .slotID); generatorID = try c.decode(String.self, forKey: .generatorID)
        canonicalOrdinal = try c.decode(Int.self, forKey: .canonicalOrdinal)
        duplicateOrdinal = try c.decode(Int.self, forKey: .duplicateOrdinal)
        selectionScopeSHA256 = try c.decode(String.self, forKey: .selectionScopeSHA256)
        cacheEpoch = try c.decode(Int.self, forKey: .cacheEpoch)
        draftArtifactID = try c.decodeIfPresent(String.self, forKey: .draftArtifactID)
        guard isWellFormed else {
            throw DecodingError.dataCorruptedError(forKey: .slotID, in: c, debugDescription: "Invalid canonical slot")
        }
    }
    public var isWellFormed: Bool {
        ProcessingIdentifier.slot(slotID) && MinutesEnsembleGenerator.isValidID(generatorID)
            && (0...3).contains(canonicalOrdinal) && (0...3).contains(duplicateOrdinal)
            && ProcessingIdentifier.sha256(selectionScopeSHA256) && cacheEpoch >= 0
            && (draftArtifactID.map(ProcessingIdentifier.artifact) ?? true)
    }
}

public struct ProcessingDraftArtifactBinding: Codable, Equatable, Sendable {
    public let generatorID: String
    public let artifactID: String
    enum CodingKeys: String, CodingKey, CaseIterable { case generatorID = "generator_id", artifactID = "artifact_id" }
    public init(from decoder: Decoder) throws {
        try requireContractKeys(decoder, allowed: Set(CodingKeys.allCases.map(\.rawValue)),
                                required: Set(CodingKeys.allCases.map(\.rawValue)))
        let c = try decoder.container(keyedBy: CodingKeys.self)
        generatorID = try c.decode(String.self, forKey: .generatorID)
        artifactID = try c.decode(String.self, forKey: .artifactID)
        guard isWellFormed else {
            throw DecodingError.dataCorruptedError(forKey: .artifactID, in: c, debugDescription: "Invalid draft binding")
        }
    }
    public var isWellFormed: Bool {
        MinutesEnsembleGenerator.isValidID(generatorID) && ProcessingIdentifier.artifact(artifactID)
    }
}

public struct ProcessingRunSummary: Codable, Equatable, Sendable, Identifiable {
    public let runID: String
    public let kind: String
    public let status: ProcessingRunStatus
    public let createdAt: String
    public let updatedAt: String
    public let generatorCount: Int
    public let completedGenerators: Int
    public let attemptsUsed: Int
    public let attemptLimit: Int
    public let publishedVersion: Int?
    public let blockedCalls: Int
    public var id: String { runID }
    enum CodingKeys: String, CodingKey, CaseIterable {
        case runID = "run_id", kind, status, createdAt = "created_at", updatedAt = "updated_at"
        case generatorCount = "generator_count", completedGenerators = "completed_generators"
        case attemptsUsed = "attempts_used", attemptLimit = "attempt_limit"
        case publishedVersion = "published_version", blockedCalls = "blocked_calls"
    }
    public init(from decoder: Decoder) throws {
        try requireContractKeys(decoder, allowed: Set(CodingKeys.allCases.map(\.rawValue)),
                                required: Set(CodingKeys.allCases.map(\.rawValue)))
        let c = try decoder.container(keyedBy: CodingKeys.self)
        runID = try c.decode(String.self, forKey: .runID); kind = try c.decode(String.self, forKey: .kind)
        status = try c.decode(ProcessingRunStatus.self, forKey: .status)
        createdAt = try c.decode(String.self, forKey: .createdAt); updatedAt = try c.decode(String.self, forKey: .updatedAt)
        generatorCount = try c.decode(Int.self, forKey: .generatorCount)
        completedGenerators = try c.decode(Int.self, forKey: .completedGenerators)
        attemptsUsed = try c.decode(Int.self, forKey: .attemptsUsed); attemptLimit = try c.decode(Int.self, forKey: .attemptLimit)
        publishedVersion = try c.decodeIfPresent(Int.self, forKey: .publishedVersion)
        blockedCalls = try c.decode(Int.self, forKey: .blockedCalls)
        guard isWellFormed else {
            throw DecodingError.dataCorruptedError(forKey: .runID, in: c, debugDescription: "Invalid run summary")
        }
    }
    public var isWellFormed: Bool {
        ProcessingIdentifier.run(runID) && kind == "minutes_ensemble" && (2...4).contains(generatorCount)
            && (0...generatorCount).contains(completedGenerators) && (0...64).contains(attemptsUsed)
            && attemptLimit == 64 && (publishedVersion.map { $0 >= 1 } ?? true) && (0...4096).contains(blockedCalls)
    }
}

public struct ProcessingRun: Codable, Equatable, Sendable, Identifiable {
    public let runID: String
    public let kind: String
    public let status: ProcessingRunStatus
    public let ensemble: MinutesEnsembleSelection
    public let externalSendPolicy: String
    public let inputArtifactID: String
    public let configurationSHA256: String
    public let canonicalSlots: [ProcessingCanonicalSlot]
    public let attemptsUsed: Int
    public let attemptLimit: Int
    public let nodes: [ProcessingNode]
    public let draftArtifactIDs: [ProcessingDraftArtifactBinding]
    public let synthesisArtifactID: String?
    public let publishedVersion: Int?
    public let blocked: [MinutesOutcomeUnknownDetails]
    public let error: ProcessingSafeError?
    public let createdAt: String
    public let updatedAt: String
    public var id: String { runID }
    enum CodingKeys: String, CodingKey, CaseIterable {
        case runID = "run_id", kind, status, ensemble, externalSendPolicy = "external_send_policy"
        case inputArtifactID = "input_artifact_id", configurationSHA256 = "configuration_sha256"
        case canonicalSlots = "canonical_slots", attemptsUsed = "attempts_used", attemptLimit = "attempt_limit"
        case nodes, draftArtifactIDs = "draft_artifact_ids", synthesisArtifactID = "synthesis_artifact_id"
        case publishedVersion = "published_version", blocked, error, createdAt = "created_at", updatedAt = "updated_at"
    }

    public init(from decoder: Decoder) throws {
        try requireContractKeys(decoder, allowed: Set(CodingKeys.allCases.map(\.rawValue)),
                                required: Set(CodingKeys.allCases.map(\.rawValue)))
        let c = try decoder.container(keyedBy: CodingKeys.self)
        runID = try c.decode(String.self, forKey: .runID); kind = try c.decode(String.self, forKey: .kind)
        status = try c.decode(ProcessingRunStatus.self, forKey: .status)
        ensemble = try c.decode(MinutesEnsembleSelection.self, forKey: .ensemble)
        externalSendPolicy = try c.decode(String.self, forKey: .externalSendPolicy)
        inputArtifactID = try c.decode(String.self, forKey: .inputArtifactID)
        configurationSHA256 = try c.decode(String.self, forKey: .configurationSHA256)
        canonicalSlots = try c.decode([ProcessingCanonicalSlot].self, forKey: .canonicalSlots)
        attemptsUsed = try c.decode(Int.self, forKey: .attemptsUsed); attemptLimit = try c.decode(Int.self, forKey: .attemptLimit)
        nodes = try c.decode([ProcessingNode].self, forKey: .nodes)
        draftArtifactIDs = try c.decode([ProcessingDraftArtifactBinding].self, forKey: .draftArtifactIDs)
        synthesisArtifactID = try c.decodeIfPresent(String.self, forKey: .synthesisArtifactID)
        publishedVersion = try c.decodeIfPresent(Int.self, forKey: .publishedVersion)
        blocked = try c.decode([MinutesOutcomeUnknownDetails].self, forKey: .blocked)
        error = try c.decodeIfPresent(ProcessingSafeError.self, forKey: .error)
        createdAt = try c.decode(String.self, forKey: .createdAt); updatedAt = try c.decode(String.self, forKey: .updatedAt)
        guard isWellFormed else {
            throw DecodingError.dataCorruptedError(forKey: .runID, in: c, debugDescription: "Invalid processing run")
        }
    }

    public var isWellFormed: Bool {
        guard ProcessingIdentifier.run(runID), kind == "minutes_ensemble", ensemble.isWellFormed,
              ["local_only", "subscription_ok", "api_ok"].contains(externalSendPolicy),
              ProcessingIdentifier.artifact(inputArtifactID), ProcessingIdentifier.sha256(configurationSHA256),
              canonicalSlots.count == ensemble.generators.count, canonicalSlots.allSatisfy(\.isWellFormed),
              Set(canonicalSlots.map(\.slotID)).count == canonicalSlots.count,
              Set(canonicalSlots.map(\.generatorID)) == Set(ensemble.generators.map(\.id)),
              canonicalSlotOrderIsConsistent,
              (0...64).contains(attemptsUsed), attemptLimit == 64, nodes.count <= 4096,
              Set(nodes.map(\.nodeID)).count == nodes.count, nodes.allSatisfy(\.isWellFormed),
              draftArtifactIDs.count <= 8192, draftArtifactIDs.allSatisfy(\.isWellFormed),
              synthesisArtifactID.map(ProcessingIdentifier.artifact) ?? true,
              publishedVersion.map({ $0 >= 1 }) ?? true, blocked.count <= 4096,
              nodeOriginsMatchRun, artifactBindingsAreConsistent else { return false }
        let nodeIDs = Set(nodes.map(\.nodeID))
        let nodesByID = Dictionary(uniqueKeysWithValues: nodes.map { ($0.nodeID, $0) })
        let slotsByID = Dictionary(uniqueKeysWithValues: canonicalSlots.map { ($0.slotID, $0) })
        guard nodes.allSatisfy({ node in
                  Set(node.dependencyNodeIDs).isSubset(of: nodeIDs)
                      && (node.role != .generator || slotsByID[node.slotID ?? ""]?.generatorID == node.generatorID)
              }),
              blocked.allSatisfy({ details in
                  guard let node = nodesByID[details.target.nodeID] else { return false }
                  return details.target.runID == runID && node.callID == details.target.callID
                      && node.status == .unknown && node.origin == details.origin
                      && node.contentFingerprint == details.contentFingerprint
              }) else { return false }
        return !hasDependencyCycle(nodes: nodes)
    }

    private func hasDependencyCycle(nodes: [ProcessingNode]) -> Bool {
        let dependencies = Dictionary(uniqueKeysWithValues: nodes.map { ($0.nodeID, $0.dependencyNodeIDs) })
        var visiting = Set<String>(), visited = Set<String>()
        func visit(_ id: String) -> Bool {
            if visiting.contains(id) { return true }
            if visited.contains(id) { return false }
            visiting.insert(id)
            for dependency in dependencies[id] ?? [] where visit(dependency) { return true }
            visiting.remove(id); visited.insert(id); return false
        }
        return nodes.contains { visit($0.nodeID) }
    }
}

public struct ListProcessingRunsResponse: Codable, Equatable, Sendable {
    public let meetingID: String
    public let runs: [ProcessingRunSummary]
    public let nextCursor: String?
    enum CodingKeys: String, CodingKey, CaseIterable { case meetingID = "meeting_id", runs, nextCursor = "next_cursor" }
    public init(from decoder: Decoder) throws {
        try requireContractKeys(decoder, allowed: Set(CodingKeys.allCases.map(\.rawValue)),
                                required: Set(CodingKeys.allCases.map(\.rawValue)))
        let c = try decoder.container(keyedBy: CodingKeys.self)
        meetingID = try c.decode(String.self, forKey: .meetingID)
        runs = try c.decode([ProcessingRunSummary].self, forKey: .runs)
        nextCursor = try c.decodeIfPresent(String.self, forKey: .nextCursor)
        let cursorValid = nextCursor.map {
            (1...256).contains($0.count)
                && $0.utf8.allSatisfy { (48...57).contains($0) || (65...90).contains($0)
                    || (97...122).contains($0) || $0 == 45 || $0 == 95 }
        } ?? true
        guard runs.count <= 100, runs.allSatisfy(\.isWellFormed), cursorValid else {
            throw DecodingError.dataCorruptedError(forKey: .runs, in: c, debugDescription: "Invalid processing run list")
        }
    }
}

public struct GetProcessingRunResponse: Codable, Equatable, Sendable {
    public let meetingID: String
    public let run: ProcessingRun
    enum CodingKeys: String, CodingKey, CaseIterable { case meetingID = "meeting_id", run }
    public init(from decoder: Decoder) throws {
        try requireContractKeys(decoder, allowed: Set(CodingKeys.allCases.map(\.rawValue)),
                                required: Set(CodingKeys.allCases.map(\.rawValue)))
        let c = try decoder.container(keyedBy: CodingKeys.self)
        meetingID = try c.decode(String.self, forKey: .meetingID)
        run = try c.decode(ProcessingRun.self, forKey: .run)
    }
}

/// Known keys of a succeeded job result. The server may return additional result fields.
public struct JobResultSummary: Codable, Equatable, Sendable {
    public var meetingID: String?
    public var minutesVersion: Int?
    public var destination: String?
    public var ref: String?
    public var stages: [String]?
    public var processingRunID: String?
    public private(set) var processingRunIDWasPresent: Bool

    enum CodingKeys: String, CodingKey {
        case meetingID = "meeting_id", minutesVersion = "minutes_version", destination, ref
        case stages, processingRunID = "processing_run_id"
    }

    public init(
        meetingID: String? = nil, minutesVersion: Int? = nil, destination: String? = nil,
        ref: String? = nil, stages: [String]? = nil,
        processingRunID: String? = nil, processingRunIDWasPresent: Bool = true
    ) {
        self.meetingID = meetingID; self.minutesVersion = minutesVersion; self.destination = destination
        self.ref = ref; self.stages = stages; self.processingRunID = processingRunID
        self.processingRunIDWasPresent = processingRunIDWasPresent
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        self.init(meetingID: try c.decodeIfPresent(String.self, forKey: .meetingID),
                  minutesVersion: try c.decodeIfPresent(Int.self, forKey: .minutesVersion),
                  destination: try c.decodeIfPresent(String.self, forKey: .destination),
                  ref: try c.decodeIfPresent(String.self, forKey: .ref),
                  stages: try c.decodeIfPresent([String].self, forKey: .stages),
                  processingRunID: try c.decodeIfPresent(String.self, forKey: .processingRunID),
                  processingRunIDWasPresent: c.contains(.processingRunID))
    }

    public func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encodeIfPresent(meetingID, forKey: .meetingID); try c.encodeIfPresent(minutesVersion, forKey: .minutesVersion)
        try c.encodeIfPresent(destination, forKey: .destination); try c.encodeIfPresent(ref, forKey: .ref)
        try c.encodeIfPresent(stages, forKey: .stages)
        if processingRunIDWasPresent { try c.encode(processingRunID, forKey: .processingRunID) }
    }
}

/// `defs/common.json#/$defs/job`, with contract-major-aware required-nullable tracking.
public struct Job: Codable, Equatable, Sendable, Identifiable {
    public var jobID: String
    public var meetingID: String?
    public var kind: String
    public var status: String
    public var processingRunID: String?
    public private(set) var processingRunIDWasPresent: Bool
    public var progress: JobProgress?
    public var result: JobResultSummary?
    public var error: ToolErrorInfo?
    public var createdAt: String
    public var updatedAt: String
    public var id: String { jobID }
    public var isActive: Bool { status == "queued" || status == "running" }

    enum CodingKeys: String, CodingKey, CaseIterable {
        case jobID = "job_id", meetingID = "meeting_id", kind, status
        case processingRunID = "processing_run_id", progress, result, error
        case createdAt = "created_at", updatedAt = "updated_at"
    }

    public init(
        jobID: String, meetingID: String? = nil, kind: String, status: String,
        processingRunID: String? = nil, processingRunIDWasPresent: Bool = true,
        progress: JobProgress? = nil, result: JobResultSummary? = nil, error: ToolErrorInfo? = nil,
        createdAt: String, updatedAt: String
    ) {
        self.jobID = jobID; self.meetingID = meetingID; self.kind = kind; self.status = status
        self.processingRunID = processingRunID; self.processingRunIDWasPresent = processingRunIDWasPresent
        self.progress = progress; self.result = result; self.error = error
        self.createdAt = createdAt; self.updatedAt = updatedAt
    }

    public init(from decoder: Decoder) throws {
        try requireContractKeys(decoder, allowed: Set(CodingKeys.allCases.map(\.rawValue)),
                                required: ["job_id", "kind", "status", "created_at", "updated_at"])
        let c = try decoder.container(keyedBy: CodingKeys.self)
        self.init(jobID: try c.decode(String.self, forKey: .jobID),
                  meetingID: try c.decodeIfPresent(String.self, forKey: .meetingID),
                  kind: try c.decode(String.self, forKey: .kind), status: try c.decode(String.self, forKey: .status),
                  processingRunID: try c.decodeIfPresent(String.self, forKey: .processingRunID),
                  processingRunIDWasPresent: c.contains(.processingRunID),
                  progress: try c.decodeIfPresent(JobProgress.self, forKey: .progress),
                  result: try c.decodeIfPresent(JobResultSummary.self, forKey: .result),
                  error: try c.decodeIfPresent(ToolErrorInfo.self, forKey: .error),
                  createdAt: try c.decode(String.self, forKey: .createdAt), updatedAt: try c.decode(String.self, forKey: .updatedAt))
    }

    public func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(jobID, forKey: .jobID); try c.encodeIfPresent(meetingID, forKey: .meetingID)
        try c.encode(kind, forKey: .kind); try c.encode(status, forKey: .status)
        if processingRunIDWasPresent { try c.encode(processingRunID, forKey: .processingRunID) }
        try c.encodeIfPresent(progress, forKey: .progress); try c.encodeIfPresent(result, forKey: .result)
        try c.encodeIfPresent(error, forKey: .error); try c.encode(createdAt, forKey: .createdAt)
        try c.encode(updatedAt, forKey: .updatedAt)
    }

    public func validatesProcessingRunCorrelation(contractVersion: String?) -> Bool {
        let major = contractVersion?.split(separator: ".").first
        if major == "6" {
            guard processingRunIDWasPresent else { return false }
            if status == "succeeded", result == nil { return false }
            if let result, !result.processingRunIDWasPresent { return false }
            if status == "succeeded", let result {
                if ["export", "provider_setup"].contains(kind), result.processingRunID != nil { return false }
                if kind == "regenerate", result.stages?.contains("minutes_ensemble") == true,
                    result.processingRunID == nil { return false }
            }
        }
        guard processingRunID.map(ProcessingIdentifier.run) ?? true,
              result?.processingRunID.map(ProcessingIdentifier.run) ?? true else { return false }
        if status == "succeeded", result?.processingRunID != processingRunID { return false }
        if let resultRun = result?.processingRunID, processingRunID != resultRun { return false }
        return true
    }

    public func canFollow(_ previous: Job) -> Bool {
        guard previous.jobID == jobID else { return false }
        guard let previousRun = previous.processingRunID else { return true }
        return processingRunID == previousRun
    }
}

public struct JobStatusResponse: Codable, Equatable, Sendable { public var job: Job }
