import Foundation

extension ProcessingNode {
    var statusFieldsAreConsistent: Bool {
        let executionValues = [callID != nil, contentFingerprint != nil, origin != nil]
        let hasExecution = executionValues.allSatisfy { $0 }
        let hasNoExecution = executionValues.allSatisfy { !$0 }
        guard hasExecution || hasNoExecution else { return false }
        if let retryLineage {
            guard retryLineage.isWellFormed else { return false }
            if status == .succeeded || status == .reused {
                guard retryLineage.resolvedBy == origin else { return false }
            } else if retryLineage.resolvedBy != nil {
                return false
            }
        }
        switch status {
        case .prepared:
            return hasNoExecution && artifactID == nil && retryLineage == nil && !reused && error == nil
        case .submitted:
            return hasExecution && artifactID == nil && !reused && error == nil
        case .succeeded:
            return hasExecution && artifactID != nil && !reused && error == nil
        case .reused:
            return hasExecution && artifactID != nil && reused && error == nil
        case .failed, .cancelled:
            return artifactID == nil && !reused && error != nil
        case .unknown:
            return hasExecution && artifactID == nil && !reused && error != nil
        }
    }
}

extension ProcessingRun {
    var nodeOriginsMatchRun: Bool {
        nodes.allSatisfy { node in
            guard let origin = node.origin, !node.reused, node.status != .unknown else { return true }
            return origin.runID == runID && origin.nodeID == node.nodeID && origin.callID == node.callID
        }
    }

    var canonicalSlotOrderIsConsistent: Bool {
        var nextDuplicate: [String: Int] = [:]
        var previous: ProcessingCanonicalSlot?
        for (index, slot) in canonicalSlots.enumerated() {
            guard slot.canonicalOrdinal == index else { return false }
            let group = "\(slot.selectionScopeSHA256):\(slot.cacheEpoch)"
            guard slot.duplicateOrdinal == nextDuplicate[group, default: 0] else { return false }
            nextDuplicate[group, default: 0] += 1
            if let previous {
                let ordered = previous.selectionScopeSHA256 < slot.selectionScopeSHA256
                    || (previous.selectionScopeSHA256 == slot.selectionScopeSHA256
                        && (previous.cacheEpoch < slot.cacheEpoch
                            || (previous.cacheEpoch == slot.cacheEpoch
                                && previous.duplicateOrdinal < slot.duplicateOrdinal)))
                guard ordered else { return false }
            }
            previous = slot
        }
        return true
    }

    var artifactBindingsAreConsistent: Bool {
        let slotsByID = Dictionary(uniqueKeysWithValues: canonicalSlots.map { ($0.slotID, $0) })
        let generatorIDs = Set(canonicalSlots.map(\.generatorID))
        guard draftArtifactIDs.allSatisfy({ generatorIDs.contains($0.generatorID) }) else { return false }
        let bindingPairs = draftArtifactIDs.map { "\($0.generatorID):\($0.artifactID)" }
        guard Set(bindingPairs).count == bindingPairs.count else { return false }
        for slot in canonicalSlots {
            if let artifactID = slot.draftArtifactID,
                !bindingPairs.contains("\(slot.generatorID):\(artifactID)") { return false }
        }
        for node in nodes where node.status == .succeeded || node.status == .reused {
            if node.role == .generator, node.phase == .final {
                guard let slotID = node.slotID, let slot = slotsByID[slotID],
                    slot.draftArtifactID == node.artifactID else { return false }
            }
            if node.role == .synthesizer, node.phase == .final,
                synthesisArtifactID != node.artifactID { return false }
        }
        for slot in canonicalSlots where slot.draftArtifactID != nil {
            guard nodes.contains(where: {
                $0.role == .generator && $0.phase == .final && $0.slotID == slot.slotID
                    && ($0.status == .succeeded || $0.status == .reused)
                    && $0.artifactID == slot.draftArtifactID
            }) else { return false }
        }
        if let synthesisArtifactID {
            guard nodes.contains(where: {
                $0.role == .synthesizer && $0.phase == .final
                    && ($0.status == .succeeded || $0.status == .reused)
                    && $0.artifactID == synthesisArtifactID
            }) else { return false }
        }
        return true
    }
}
