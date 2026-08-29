import Foundation

public enum ProcessingArtifactKind: String, Codable, CaseIterable, Sendable {
    case sourceIndex = "source_index"
    case source
    case draftChunk = "draft_chunk"
    case draft
    case synthesis
}

public struct ProcessingGenerationUsage: Codable, Equatable, Sendable {
    public let inputTokens: Int?
    public let outputTokens: Int?
    public let totalTokens: Int?
    public let cachedInputTokens: Int?
    public let cacheWriteInputTokens: Int?
    public let reasoningOutputTokens: Int?

    enum CodingKeys: String, CodingKey, CaseIterable {
        case inputTokens = "input_tokens", outputTokens = "output_tokens", totalTokens = "total_tokens"
        case cachedInputTokens = "cached_input_tokens", cacheWriteInputTokens = "cache_write_input_tokens"
        case reasoningOutputTokens = "reasoning_output_tokens"
    }

    public init(
        inputTokens: Int? = nil, outputTokens: Int? = nil, totalTokens: Int? = nil,
        cachedInputTokens: Int? = nil, cacheWriteInputTokens: Int? = nil,
        reasoningOutputTokens: Int? = nil
    ) {
        self.inputTokens = inputTokens; self.outputTokens = outputTokens; self.totalTokens = totalTokens
        self.cachedInputTokens = cachedInputTokens; self.cacheWriteInputTokens = cacheWriteInputTokens
        self.reasoningOutputTokens = reasoningOutputTokens
    }

    public init(from decoder: Decoder) throws {
        try requireContractKeys(decoder, allowed: Set(CodingKeys.allCases.map(\.rawValue)), required: [])
        let c = try decoder.container(keyedBy: CodingKeys.self)
        self.init(inputTokens: try c.decodeIfPresent(Int.self, forKey: .inputTokens),
                  outputTokens: try c.decodeIfPresent(Int.self, forKey: .outputTokens),
                  totalTokens: try c.decodeIfPresent(Int.self, forKey: .totalTokens),
                  cachedInputTokens: try c.decodeIfPresent(Int.self, forKey: .cachedInputTokens),
                  cacheWriteInputTokens: try c.decodeIfPresent(Int.self, forKey: .cacheWriteInputTokens),
                  reasoningOutputTokens: try c.decodeIfPresent(Int.self, forKey: .reasoningOutputTokens))
        guard isWellFormed else {
            throw DecodingError.dataCorruptedError(forKey: .inputTokens, in: c, debugDescription: "Invalid generation usage")
        }
    }

    public var isWellFormed: Bool {
        let values = [inputTokens, outputTokens, totalTokens, cachedInputTokens, cacheWriteInputTokens, reasoningOutputTokens]
        return values.contains(where: { $0 != nil })
            && values.compactMap { $0 }.allSatisfy { (0...9_007_199_254_740_991).contains($0) }
    }
}

public enum ProcessingDataDestination: String, Codable, Sendable { case local, openai, anthropic }
public enum ProcessingCostClass: String, Codable, Sendable { case local, subscription, api }

public struct ProcessingGenerationMetadata: Codable, Equatable, Sendable {
    public let requestedSelection: MinutesModelSelection
    public let effectiveParameters: MinutesModelSelection.Parameters
    public let returnedModel: String?
    public let usage: ProcessingGenerationUsage?
    public let dataDestination: ProcessingDataDestination
    public let costClass: ProcessingCostClass
    public let retryLineage: ProcessingRetryLineage?

    enum CodingKeys: String, CodingKey, CaseIterable {
        case requestedSelection = "requested_selection", effectiveParameters = "effective_parameters"
        case returnedModel = "returned_model", usage, dataDestination = "data_destination"
        case costClass = "cost_class", retryLineage = "retry_lineage"
    }

    public init(from decoder: Decoder) throws {
        try requireContractKeys(decoder, allowed: Set(CodingKeys.allCases.map(\.rawValue)),
                                required: Set(CodingKeys.allCases.map(\.rawValue)))
        let c = try decoder.container(keyedBy: CodingKeys.self)
        requestedSelection = try c.decode(MinutesModelSelection.self, forKey: .requestedSelection)
        effectiveParameters = try c.decode(MinutesModelSelection.Parameters.self, forKey: .effectiveParameters)
        returnedModel = try c.decodeIfPresent(String.self, forKey: .returnedModel)
        usage = try c.decodeIfPresent(ProcessingGenerationUsage.self, forKey: .usage)
        dataDestination = try c.decode(ProcessingDataDestination.self, forKey: .dataDestination)
        costClass = try c.decode(ProcessingCostClass.self, forKey: .costClass)
        retryLineage = try c.decodeIfPresent(ProcessingRetryLineage.self, forKey: .retryLineage)
        guard isWellFormed else {
            throw DecodingError.dataCorruptedError(forKey: .requestedSelection, in: c, debugDescription: "Invalid generation metadata")
        }
    }

    public var isWellFormed: Bool {
        guard returnedModel.map({ (1...256).contains($0.count) }) ?? true else { return false }
        switch requestedSelection.provider {
        case "codex-app-server":
            return dataDestination == .openai && costClass == .subscription && effectiveParameters.maxTokens == nil
        case "openai-api":
            return dataDestination == .openai && costClass == .api
        case "anthropic-api":
            return dataDestination == .anthropic && costClass == .api && effectiveParameters.reasoningEffort == nil
        case "ollama":
            return dataDestination == .local && costClass == .local && effectiveParameters.reasoningEffort == nil
        default: return false
        }
    }
}

public struct ProcessingArtifactHeader: Codable, Equatable, Sendable, Identifiable {
    public let artifactID: String
    public let runID: String
    public let nodeID: String?
    public let kind: ProcessingArtifactKind
    public let bodySHA256: String
    public let sourceArtifactIDs: [String]
    public let origin: ProcessingOriginRef?
    public let generation: ProcessingGenerationMetadata?
    public let createdAt: String
    public var id: String { artifactID }

    enum CodingKeys: String, CodingKey, CaseIterable {
        case artifactID = "artifact_id", runID = "run_id", nodeID = "node_id", kind
        case bodySHA256 = "body_sha256", sourceArtifactIDs = "source_artifact_ids"
        case origin, generation, createdAt = "created_at"
    }

    public init(from decoder: Decoder) throws {
        try requireContractKeys(decoder, allowed: Set(CodingKeys.allCases.map(\.rawValue)),
                                required: Set(CodingKeys.allCases.map(\.rawValue)))
        let c = try decoder.container(keyedBy: CodingKeys.self)
        artifactID = try c.decode(String.self, forKey: .artifactID); runID = try c.decode(String.self, forKey: .runID)
        nodeID = try c.decodeIfPresent(String.self, forKey: .nodeID); kind = try c.decode(ProcessingArtifactKind.self, forKey: .kind)
        bodySHA256 = try c.decode(String.self, forKey: .bodySHA256)
        sourceArtifactIDs = try c.decode([String].self, forKey: .sourceArtifactIDs)
        origin = try c.decodeIfPresent(ProcessingOriginRef.self, forKey: .origin)
        generation = try c.decodeIfPresent(ProcessingGenerationMetadata.self, forKey: .generation)
        createdAt = try c.decode(String.self, forKey: .createdAt)
        guard isWellFormed else {
            throw DecodingError.dataCorruptedError(forKey: .artifactID, in: c, debugDescription: "Invalid artifact header")
        }
    }

    public var isWellFormed: Bool {
        guard ProcessingIdentifier.artifact(artifactID), ProcessingIdentifier.run(runID),
              nodeID.map(ProcessingIdentifier.node) ?? true, ProcessingIdentifier.sha256(bodySHA256),
              sourceArtifactIDs.count <= 64, Set(sourceArtifactIDs).count == sourceArtifactIDs.count,
              sourceArtifactIDs.allSatisfy(ProcessingIdentifier.artifact) else { return false }
        switch kind {
        case .sourceIndex, .source, .draft: return origin == nil && generation == nil
        case .draftChunk, .synthesis:
            guard let origin, let generation else { return false }
            let selected = generation.requestedSelection
            return nodeID != nil && origin.provider == selected.provider
                && origin.connectionID == selected.connectionID
                && origin.connectionRevision == selected.connectionRevision && origin.modelID == selected.modelID
                && (generation.retryLineage?.resolvedBy == origin || generation.retryLineage == nil)
        }
    }
}

public struct ProcessingArtifactDependencyMapping: Codable, Equatable, Sendable {
    public let originArtifactID: String
    public let currentArtifactID: String
    public let contentProjectionSHA256: String
    enum CodingKeys: String, CodingKey, CaseIterable {
        case originArtifactID = "origin_artifact_id", currentArtifactID = "current_artifact_id"
        case contentProjectionSHA256 = "content_projection_sha256"
    }
    public init(from decoder: Decoder) throws {
        try requireContractKeys(decoder, allowed: Set(CodingKeys.allCases.map(\.rawValue)),
                                required: Set(CodingKeys.allCases.map(\.rawValue)))
        let c = try decoder.container(keyedBy: CodingKeys.self)
        originArtifactID = try c.decode(String.self, forKey: .originArtifactID)
        currentArtifactID = try c.decode(String.self, forKey: .currentArtifactID)
        contentProjectionSHA256 = try c.decode(String.self, forKey: .contentProjectionSHA256)
        guard isWellFormed else {
            throw DecodingError.dataCorruptedError(forKey: .originArtifactID, in: c, debugDescription: "Invalid dependency mapping")
        }
    }
    public var isWellFormed: Bool {
        ProcessingIdentifier.artifact(originArtifactID) && ProcessingIdentifier.artifact(currentArtifactID)
            && ProcessingIdentifier.sha256(contentProjectionSHA256)
    }
}

public struct ProcessingArtifactBinding: Codable, Equatable, Sendable {
    public let runID: String
    public let artifactID: String
    public let reused: Bool
    public let dependencyMappings: [ProcessingArtifactDependencyMapping]
    public let authorizationSnapshotID: String
    public let retryLineage: ProcessingRetryLineage?
    enum CodingKeys: String, CodingKey, CaseIterable {
        case runID = "run_id", artifactID = "artifact_id", reused
        case dependencyMappings = "dependency_mappings", authorizationSnapshotID = "authorization_snapshot_id"
        case retryLineage = "retry_lineage"
    }
    public init(from decoder: Decoder) throws {
        try requireContractKeys(decoder, allowed: Set(CodingKeys.allCases.map(\.rawValue)),
                                required: Set(CodingKeys.allCases.map(\.rawValue)))
        let c = try decoder.container(keyedBy: CodingKeys.self)
        runID = try c.decode(String.self, forKey: .runID); artifactID = try c.decode(String.self, forKey: .artifactID)
        reused = try c.decode(Bool.self, forKey: .reused)
        dependencyMappings = try c.decode([ProcessingArtifactDependencyMapping].self, forKey: .dependencyMappings)
        authorizationSnapshotID = try c.decode(String.self, forKey: .authorizationSnapshotID)
        retryLineage = try c.decodeIfPresent(ProcessingRetryLineage.self, forKey: .retryLineage)
        guard isWellFormed else {
            throw DecodingError.dataCorruptedError(forKey: .artifactID, in: c, debugDescription: "Invalid artifact binding")
        }
    }
    public var isWellFormed: Bool {
        ProcessingIdentifier.run(runID) && ProcessingIdentifier.artifact(artifactID)
            && dependencyMappings.count <= 64 && dependencyMappings.allSatisfy(\.isWellFormed)
            && ProcessingIdentifier.matches(authorizationSnapshotID, prefix: "auth-", hexCount: 32)
    }
}

public struct PublishedEnsembleGenerator: Codable, Equatable, Sendable, Identifiable {
    public let generatorID: String
    public let label: String
    public let selection: MinutesModelSelection
    public let artifactID: String
    public let reused: Bool
    public var id: String { generatorID }
    enum CodingKeys: String, CodingKey, CaseIterable {
        case generatorID = "generator_id", label, selection, artifactID = "artifact_id", reused
    }
    public init(from decoder: Decoder) throws {
        try requireContractKeys(decoder, allowed: Set(CodingKeys.allCases.map(\.rawValue)),
                                required: Set(CodingKeys.allCases.map(\.rawValue)))
        let c = try decoder.container(keyedBy: CodingKeys.self)
        generatorID = try c.decode(String.self, forKey: .generatorID); label = try c.decode(String.self, forKey: .label)
        selection = try c.decode(MinutesModelSelection.self, forKey: .selection)
        artifactID = try c.decode(String.self, forKey: .artifactID); reused = try c.decode(Bool.self, forKey: .reused)
        guard isWellFormed else {
            throw DecodingError.dataCorruptedError(forKey: .generatorID, in: c, debugDescription: "Invalid published generator")
        }
    }
    public var isWellFormed: Bool {
        MinutesEnsembleGenerator.isValidID(generatorID) && (1...80).contains(label.unicodeScalars.count)
            && label.range(of: #"\S"#, options: .regularExpression) != nil
            && selection.isWellFormed && ProcessingIdentifier.artifact(artifactID)
    }
}

public struct PublishedEnsembleSynthesizer: Codable, Equatable, Sendable {
    public let selection: MinutesModelSelection
    public let artifactID: String
    public let reused: Bool
    public var isWellFormed: Bool { selection.isWellFormed && ProcessingIdentifier.artifact(artifactID) }
    enum CodingKeys: String, CodingKey, CaseIterable { case selection, artifactID = "artifact_id", reused }
    public init(from decoder: Decoder) throws {
        try requireContractKeys(decoder, allowed: Set(CodingKeys.allCases.map(\.rawValue)),
                                required: Set(CodingKeys.allCases.map(\.rawValue)))
        let c = try decoder.container(keyedBy: CodingKeys.self)
        selection = try c.decode(MinutesModelSelection.self, forKey: .selection)
        artifactID = try c.decode(String.self, forKey: .artifactID); reused = try c.decode(Bool.self, forKey: .reused)
        guard isWellFormed else {
            throw DecodingError.dataCorruptedError(forKey: .artifactID, in: c, debugDescription: "Invalid published synthesizer")
        }
    }
}

public struct PublishedMinutesEnsembleProvenance: Codable, Equatable, Sendable {
    public let kind: String
    public let runID: String
    public let inputArtifactID: String
    public let generators: [PublishedEnsembleGenerator]
    public let synthesizer: PublishedEnsembleSynthesizer
    enum CodingKeys: String, CodingKey, CaseIterable {
        case kind, runID = "run_id", inputArtifactID = "input_artifact_id", generators, synthesizer
    }
    public init(from decoder: Decoder) throws {
        try requireContractKeys(decoder, allowed: Set(CodingKeys.allCases.map(\.rawValue)),
                                required: Set(CodingKeys.allCases.map(\.rawValue)))
        let c = try decoder.container(keyedBy: CodingKeys.self)
        kind = try c.decode(String.self, forKey: .kind); runID = try c.decode(String.self, forKey: .runID)
        inputArtifactID = try c.decode(String.self, forKey: .inputArtifactID)
        generators = try c.decode([PublishedEnsembleGenerator].self, forKey: .generators)
        synthesizer = try c.decode(PublishedEnsembleSynthesizer.self, forKey: .synthesizer)
        guard isWellFormed else {
            throw DecodingError.dataCorruptedError(forKey: .kind, in: c, debugDescription: "Invalid minutes provenance")
        }
    }
    public var isWellFormed: Bool {
        kind == "ensemble" && ProcessingIdentifier.run(runID) && ProcessingIdentifier.artifact(inputArtifactID)
            && (2...4).contains(generators.count) && generators.allSatisfy(\.isWellFormed)
            && Set(generators.map(\.generatorID)).count == generators.count && synthesizer.isWellFormed
    }
}

private enum EnsembleDocumentID {
    static func evidence(_ value: String) -> Bool { ProcessingIdentifier.matches(value, prefix: "ev_", hexCount: 64) }
    static func claim(_ value: String) -> Bool { ProcessingIdentifier.matches(value, prefix: "cl_", hexCount: 64) }
    static func question(_ value: String) -> Bool { ProcessingIdentifier.matches(value, prefix: "qu_", hexCount: 64) }
}

public struct EnsembleSourceBinding: Codable, Equatable, Sendable {
    public let segmentIndex: Int
    public let segmentID: String
    public let segmentTextSHA256: String
    public let sources: [String]
    enum CodingKeys: String, CodingKey, CaseIterable {
        case segmentIndex = "segment_index", segmentID = "segment_id"
        case segmentTextSHA256 = "segment_text_sha256", sources
    }
    public init(from decoder: Decoder) throws {
        try requireContractKeys(decoder, allowed: Set(CodingKeys.allCases.map(\.rawValue)),
                                required: Set(CodingKeys.allCases.map(\.rawValue)))
        let c = try decoder.container(keyedBy: CodingKeys.self)
        segmentIndex = try c.decode(Int.self, forKey: .segmentIndex); segmentID = try c.decode(String.self, forKey: .segmentID)
        segmentTextSHA256 = try c.decode(String.self, forKey: .segmentTextSHA256)
        sources = try c.decode([String].self, forKey: .sources)
        guard isWellFormed else {
            throw DecodingError.dataCorruptedError(forKey: .segmentID, in: c, debugDescription: "Invalid source binding")
        }
    }
    public var isWellFormed: Bool {
        segmentIndex >= 0 && (1...512).contains(segmentID.count) && ProcessingIdentifier.sha256(segmentTextSHA256)
            && sources.count <= 256 && sources.allSatisfy { (1...512).contains($0.count) }
    }
}

public struct EnsembleEvidence: Codable, Equatable, Sendable, Identifiable {
    public let evidenceID: String
    public let startSeconds: Double
    public let endSeconds: Double
    public let speakerLabel: String?
    public let speakerName: String?
    public let charStart: Int
    public let charEnd: Int
    public let text: String
    public let occurrenceIndex: Int
    public let occurrenceCount: Int
    public let sourceBinding: EnsembleSourceBinding
    public var id: String { evidenceID }
    enum CodingKeys: String, CodingKey, CaseIterable {
        case evidenceID = "evidence_id", startSeconds = "start_seconds", endSeconds = "end_seconds"
        case speakerLabel = "speaker_label", speakerName = "speaker_name"
        case charStart = "char_start", charEnd = "char_end", text
        case occurrenceIndex = "occurrence_index", occurrenceCount = "occurrence_count"
        case sourceBinding = "source_binding"
    }
    public init(from decoder: Decoder) throws {
        try requireContractKeys(decoder, allowed: Set(CodingKeys.allCases.map(\.rawValue)),
                                required: Set(CodingKeys.allCases.map(\.rawValue)))
        let c = try decoder.container(keyedBy: CodingKeys.self)
        evidenceID = try c.decode(String.self, forKey: .evidenceID)
        startSeconds = try c.decode(Double.self, forKey: .startSeconds); endSeconds = try c.decode(Double.self, forKey: .endSeconds)
        speakerLabel = try c.decodeIfPresent(String.self, forKey: .speakerLabel)
        speakerName = try c.decodeIfPresent(String.self, forKey: .speakerName)
        charStart = try c.decode(Int.self, forKey: .charStart); charEnd = try c.decode(Int.self, forKey: .charEnd)
        text = try c.decode(String.self, forKey: .text); occurrenceIndex = try c.decode(Int.self, forKey: .occurrenceIndex)
        occurrenceCount = try c.decode(Int.self, forKey: .occurrenceCount)
        sourceBinding = try c.decode(EnsembleSourceBinding.self, forKey: .sourceBinding)
        guard isWellFormed else {
            throw DecodingError.dataCorruptedError(forKey: .evidenceID, in: c, debugDescription: "Invalid ensemble evidence")
        }
    }
    public var isWellFormed: Bool {
        EnsembleDocumentID.evidence(evidenceID) && startSeconds.isFinite && endSeconds.isFinite
            && startSeconds >= 0 && endSeconds >= startSeconds
            && (speakerLabel.map { $0.count <= 512 } ?? true) && (speakerName.map { $0.count <= 512 } ?? true)
            && charStart >= 0 && charEnd > charStart && (1...512).contains(text.count)
            && occurrenceCount >= 1 && (0..<occurrenceCount).contains(occurrenceIndex) && sourceBinding.isWellFormed
    }
}

public struct EnsembleEvidenceRef: Codable, Equatable, Sendable {
    public let evidenceID: String
    public let charStart: Int
    public let charEnd: Int
    enum CodingKeys: String, CodingKey, CaseIterable {
        case evidenceID = "evidence_id", charStart = "char_start", charEnd = "char_end"
    }
    public init(from decoder: Decoder) throws {
        try requireContractKeys(decoder, allowed: Set(CodingKeys.allCases.map(\.rawValue)),
                                required: Set(CodingKeys.allCases.map(\.rawValue)))
        let c = try decoder.container(keyedBy: CodingKeys.self)
        evidenceID = try c.decode(String.self, forKey: .evidenceID)
        charStart = try c.decode(Int.self, forKey: .charStart); charEnd = try c.decode(Int.self, forKey: .charEnd)
        guard isWellFormed else {
            throw DecodingError.dataCorruptedError(forKey: .evidenceID, in: c, debugDescription: "Invalid evidence reference")
        }
    }
    public var isWellFormed: Bool { EnsembleDocumentID.evidence(evidenceID) && charStart >= 0 && charEnd > charStart }
}

public enum EnsembleClaimKind: String, Codable, Sendable { case agenda, discussion, decision, action }

public struct EnsembleClaim: Codable, Equatable, Sendable, Identifiable {
    public let id: String
    public let kind: EnsembleClaimKind
    public let text: String
    public let evidence: [EnsembleEvidenceRef]
    public let owner: String?
    public let due: String?
    enum CodingKeys: String, CodingKey, CaseIterable { case id, kind, text, evidence, owner, due }
    public init(from decoder: Decoder) throws {
        try requireContractKeys(decoder, allowed: Set(CodingKeys.allCases.map(\.rawValue)),
                                required: Set(CodingKeys.allCases.map(\.rawValue)))
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(String.self, forKey: .id); kind = try c.decode(EnsembleClaimKind.self, forKey: .kind)
        text = try c.decode(String.self, forKey: .text); evidence = try c.decode([EnsembleEvidenceRef].self, forKey: .evidence)
        owner = try c.decodeIfPresent(String.self, forKey: .owner); due = try c.decodeIfPresent(String.self, forKey: .due)
        guard isWellFormed else {
            throw DecodingError.dataCorruptedError(forKey: .id, in: c, debugDescription: "Invalid ensemble claim")
        }
    }
    public var isWellFormed: Bool {
        let kindFieldsValid = kind == .action || (owner == nil && due == nil)
        return kindFieldsValid && EnsembleDocumentID.claim(id) && (1...600).contains(text.count)
            && text.range(of: #"\S"#, options: .regularExpression) != nil
            && (1...8).contains(evidence.count) && evidence.allSatisfy(\.isWellFormed)
            && [owner, due].allSatisfy { $0.map { (1...120).contains($0.count) && $0.range(of: #"\S"#, options: .regularExpression) != nil } ?? true }
    }
}

public struct EnsembleQuestionAlternative: Codable, Equatable, Sendable {
    public let text: String
    public let evidence: [EnsembleEvidenceRef]
    enum CodingKeys: String, CodingKey, CaseIterable { case text, evidence }
    public init(from decoder: Decoder) throws {
        try requireContractKeys(decoder, allowed: Set(CodingKeys.allCases.map(\.rawValue)),
                                required: Set(CodingKeys.allCases.map(\.rawValue)))
        let c = try decoder.container(keyedBy: CodingKeys.self)
        text = try c.decode(String.self, forKey: .text); evidence = try c.decode([EnsembleEvidenceRef].self, forKey: .evidence)
        guard isWellFormed else {
            throw DecodingError.dataCorruptedError(forKey: .text, in: c, debugDescription: "Invalid question alternative")
        }
    }
    public var isWellFormed: Bool {
        (1...600).contains(text.count) && text.range(of: #"\S"#, options: .regularExpression) != nil
            && (1...8).contains(evidence.count) && evidence.allSatisfy(\.isWellFormed)
    }
}

public enum EnsembleQuestionKind: String, Codable, Sendable { case conflict; case missingContext = "missing_context" }

public struct EnsembleQuestion: Codable, Equatable, Sendable, Identifiable {
    public let id: String
    public let kind: EnsembleQuestionKind
    public let text: String
    public let alternatives: [EnsembleQuestionAlternative]
    enum CodingKeys: String, CodingKey, CaseIterable { case id, kind, text, alternatives }
    public init(from decoder: Decoder) throws {
        try requireContractKeys(decoder, allowed: Set(CodingKeys.allCases.map(\.rawValue)),
                                required: Set(CodingKeys.allCases.map(\.rawValue)))
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(String.self, forKey: .id); kind = try c.decode(EnsembleQuestionKind.self, forKey: .kind)
        text = try c.decode(String.self, forKey: .text)
        alternatives = try c.decode([EnsembleQuestionAlternative].self, forKey: .alternatives)
        guard isWellFormed else {
            throw DecodingError.dataCorruptedError(forKey: .id, in: c, debugDescription: "Invalid ensemble question")
        }
    }
    public var isWellFormed: Bool {
        let countRange = kind == .conflict ? 2...4 : 1...4
        return EnsembleDocumentID.question(id) && (1...600).contains(text.count)
            && text.range(of: #"\S"#, options: .regularExpression) != nil
            && countRange.contains(alternatives.count) && alternatives.allSatisfy(\.isWellFormed)
    }
}

public struct EnsembleSourceIndexDocument: Codable, Equatable, Sendable {
    public let schemaVersion: String
    public let packetArtifactIDs: [String]
    enum CodingKeys: String, CodingKey, CaseIterable {
        case schemaVersion = "schema_version", packetArtifactIDs = "packet_artifact_ids"
    }
    public init(from decoder: Decoder) throws {
        try requireContractKeys(decoder, allowed: Set(CodingKeys.allCases.map(\.rawValue)),
                                required: Set(CodingKeys.allCases.map(\.rawValue)))
        let c = try decoder.container(keyedBy: CodingKeys.self)
        schemaVersion = try c.decode(String.self, forKey: .schemaVersion)
        packetArtifactIDs = try c.decode([String].self, forKey: .packetArtifactIDs)
        guard isWellFormed else {
            throw DecodingError.dataCorruptedError(forKey: .schemaVersion, in: c, debugDescription: "Invalid source index")
        }
    }
    public var isWellFormed: Bool {
        schemaVersion == "ensemble-source-index-v1" && packetArtifactIDs.count <= 64
            && Set(packetArtifactIDs).count == packetArtifactIDs.count
            && packetArtifactIDs.allSatisfy(ProcessingIdentifier.artifact)
    }
}

public struct EnsembleSourceDocument: Codable, Equatable, Sendable {
    public let schemaVersion: String
    public let evidence: [EnsembleEvidence]
    enum CodingKeys: String, CodingKey, CaseIterable { case schemaVersion = "schema_version", evidence }
    public init(from decoder: Decoder) throws {
        try requireContractKeys(decoder, allowed: Set(CodingKeys.allCases.map(\.rawValue)),
                                required: Set(CodingKeys.allCases.map(\.rawValue)))
        let c = try decoder.container(keyedBy: CodingKeys.self)
        schemaVersion = try c.decode(String.self, forKey: .schemaVersion)
        evidence = try c.decode([EnsembleEvidence].self, forKey: .evidence)
        guard isWellFormed else {
            throw DecodingError.dataCorruptedError(forKey: .schemaVersion, in: c, debugDescription: "Invalid source document")
        }
    }
    public var isWellFormed: Bool {
        schemaVersion == "ensemble-source-v1" && (1...32).contains(evidence.count)
            && evidence.allSatisfy(\.isWellFormed) && Set(evidence.map(\.evidenceID)).count == evidence.count
    }
}

public struct EnsembleDocument: Codable, Equatable, Sendable {
    public let schemaVersion: String
    public let claims: [EnsembleClaim]
    public let questions: [EnsembleQuestion]
    enum CodingKeys: String, CodingKey, CaseIterable { case schemaVersion = "schema_version", claims, questions }
    public init(from decoder: Decoder) throws {
        try requireContractKeys(decoder, allowed: Set(CodingKeys.allCases.map(\.rawValue)),
                                required: Set(CodingKeys.allCases.map(\.rawValue)))
        let c = try decoder.container(keyedBy: CodingKeys.self)
        schemaVersion = try c.decode(String.self, forKey: .schemaVersion)
        claims = try c.decode([EnsembleClaim].self, forKey: .claims)
        questions = try c.decode([EnsembleQuestion].self, forKey: .questions)
        guard isWellFormed() else {
            throw DecodingError.dataCorruptedError(forKey: .schemaVersion, in: c, debugDescription: "Invalid ensemble document")
        }
    }
    public func isWellFormed(evidenceIDs: Set<String>? = nil) -> Bool {
        guard schemaVersion == "ensemble-document-v1", claims.count <= 32, questions.count <= 16,
              claims.allSatisfy(\.isWellFormed), questions.allSatisfy(\.isWellFormed),
              Set(claims.map(\.id)).count == claims.count, Set(questions.map(\.id)).count == questions.count else { return false }
        guard let evidenceIDs else { return true }
        let refs = claims.flatMap(\.evidence) + questions.flatMap { $0.alternatives.flatMap(\.evidence) }
        return refs.allSatisfy { evidenceIDs.contains($0.evidenceID) }
    }
}

public struct EnsembleDraftPart: Codable, Equatable, Sendable {
    public let sourceArtifactID: String
    public let documentArtifactID: String
    enum CodingKeys: String, CodingKey, CaseIterable {
        case sourceArtifactID = "source_artifact_id", documentArtifactID = "document_artifact_id"
    }
    public init(from decoder: Decoder) throws {
        try requireContractKeys(decoder, allowed: Set(CodingKeys.allCases.map(\.rawValue)),
                                required: Set(CodingKeys.allCases.map(\.rawValue)))
        let c = try decoder.container(keyedBy: CodingKeys.self)
        sourceArtifactID = try c.decode(String.self, forKey: .sourceArtifactID)
        documentArtifactID = try c.decode(String.self, forKey: .documentArtifactID)
        guard isWellFormed else {
            throw DecodingError.dataCorruptedError(forKey: .sourceArtifactID, in: c, debugDescription: "Invalid draft part")
        }
    }
    public var isWellFormed: Bool {
        ProcessingIdentifier.artifact(sourceArtifactID) && ProcessingIdentifier.artifact(documentArtifactID)
    }
}

public struct EnsembleDraftDocument: Codable, Equatable, Sendable {
    public let schemaVersion: String
    public let parts: [EnsembleDraftPart]
    enum CodingKeys: String, CodingKey, CaseIterable { case schemaVersion = "schema_version", parts }
    public init(from decoder: Decoder) throws {
        try requireContractKeys(decoder, allowed: Set(CodingKeys.allCases.map(\.rawValue)),
                                required: Set(CodingKeys.allCases.map(\.rawValue)))
        let c = try decoder.container(keyedBy: CodingKeys.self)
        schemaVersion = try c.decode(String.self, forKey: .schemaVersion)
        parts = try c.decode([EnsembleDraftPart].self, forKey: .parts)
        guard isWellFormed else {
            throw DecodingError.dataCorruptedError(forKey: .schemaVersion, in: c, debugDescription: "Invalid draft document")
        }
    }
    public var isWellFormed: Bool {
        schemaVersion == "ensemble-draft-v1" && (1...64).contains(parts.count) && parts.allSatisfy(\.isWellFormed)
    }
}

public enum ProcessingArtifactPayload: Equatable, Sendable {
    case sourceIndex(EnsembleSourceIndexDocument)
    case source(EnsembleSourceDocument)
    case draftChunk(EnsembleDocument)
    case draft(EnsembleDraftDocument)
    case synthesis(EnsembleDocument)
}

public struct ProcessingArtifactResponse: Codable, Equatable, Sendable {
    public let requestedRunID: String
    public let artifact: ProcessingArtifactHeader
    public let payload: ProcessingArtifactPayload
    public let binding: ProcessingArtifactBinding
    public let reused: Bool

    enum CodingKeys: String, CodingKey, CaseIterable {
        case requestedRunID = "requested_run_id", artifact, payload, binding, reused
    }

    public init(from decoder: Decoder) throws {
        try requireContractKeys(decoder, allowed: Set(CodingKeys.allCases.map(\.rawValue)),
                                required: Set(CodingKeys.allCases.map(\.rawValue)))
        let c = try decoder.container(keyedBy: CodingKeys.self)
        requestedRunID = try c.decode(String.self, forKey: .requestedRunID)
        artifact = try c.decode(ProcessingArtifactHeader.self, forKey: .artifact)
        binding = try c.decode(ProcessingArtifactBinding.self, forKey: .binding)
        reused = try c.decode(Bool.self, forKey: .reused)
        switch artifact.kind {
        case .sourceIndex: payload = .sourceIndex(try c.decode(EnsembleSourceIndexDocument.self, forKey: .payload))
        case .source: payload = .source(try c.decode(EnsembleSourceDocument.self, forKey: .payload))
        case .draftChunk: payload = .draftChunk(try c.decode(EnsembleDocument.self, forKey: .payload))
        case .draft: payload = .draft(try c.decode(EnsembleDraftDocument.self, forKey: .payload))
        case .synthesis: payload = .synthesis(try c.decode(EnsembleDocument.self, forKey: .payload))
        }
        guard isWellFormed else {
            throw DecodingError.dataCorruptedError(forKey: .artifact, in: c, debugDescription: "Invalid processing artifact response")
        }
    }

    public func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(requestedRunID, forKey: .requestedRunID); try c.encode(artifact, forKey: .artifact)
        try c.encode(binding, forKey: .binding); try c.encode(reused, forKey: .reused)
        switch payload {
        case .sourceIndex(let value): try c.encode(value, forKey: .payload)
        case .source(let value): try c.encode(value, forKey: .payload)
        case .draftChunk(let value), .synthesis(let value): try c.encode(value, forKey: .payload)
        case .draft(let value): try c.encode(value, forKey: .payload)
        }
    }

    public var isWellFormed: Bool {
        guard ProcessingIdentifier.run(requestedRunID), requestedRunID == binding.runID,
              artifact.artifactID == binding.artifactID, reused == binding.reused,
              reused == (artifact.runID != requestedRunID),
              artifact.isWellFormed, binding.isWellFormed,
              artifact.generation?.retryLineage == binding.retryLineage else { return false }
        switch payload {
        case .sourceIndex(let value): return artifact.kind == .sourceIndex && value.isWellFormed && binding.retryLineage == nil
        case .source(let value): return artifact.kind == .source && value.isWellFormed && binding.retryLineage == nil
        case .draftChunk(let value): return artifact.kind == .draftChunk && value.isWellFormed()
        case .draft(let value): return artifact.kind == .draft && value.isWellFormed && binding.retryLineage == nil
        case .synthesis(let value): return artifact.kind == .synthesis && value.isWellFormed()
        }
    }
}
