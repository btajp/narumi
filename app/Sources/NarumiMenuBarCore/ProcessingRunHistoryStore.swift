import Foundation
import Observation

extension ServerCapabilities {
    /// Contract 6 registers the three bounded local run-history tools. Reading past runs
    /// remains useful when the current transport or provider setup cannot start a new run.
    public func supportsProcessingRunHistory(contractVersion: String?) -> Bool {
        supportsMinutesEnsembleWire(contractVersion: contractVersion)
            && minutesEnsembleLimits?.isSupportedBaseline == true
    }
}

public protocol ProcessingRunHistoryClient: Sendable {
    func listProcessingRuns(
        meetingID: String, scope: String?, limit: Int, cursor: String?, connectionGeneration: UInt64
    ) async throws -> ListProcessingRunsResponse
    func getProcessingRun(
        meetingID: String, scope: String?, runID: String, connectionGeneration: UInt64
    ) async throws -> GetProcessingRunResponse
    func getProcessingArtifact(
        meetingID: String, scope: String?, runID: String, artifactID: String,
        connectionGeneration: UInt64
    ) async throws -> ProcessingArtifactResponse
}

public struct ProcessingArtifactGenerationPresentation: Equatable, Sendable {
    public let provider: String
    public let connectionID: String
    public let connectionRevision: Int
    public let modelID: String
    public let cacheEpoch: Int
    public let effectiveParameters: MinutesModelSelection.Parameters
    public let returnedModel: String?
    public let usage: ProcessingGenerationUsage?
    public let dataDestination: ProcessingDataDestination
    public let costClass: ProcessingCostClass

    public init(_ value: ProcessingGenerationMetadata) {
        provider = value.requestedSelection.provider
        connectionID = value.requestedSelection.connectionID
        connectionRevision = value.requestedSelection.connectionRevision
        modelID = value.requestedSelection.modelID
        cacheEpoch = value.requestedSelection.cacheEpoch
        effectiveParameters = value.effectiveParameters
        returnedModel = value.returnedModel
        usage = value.usage
        dataDestination = value.dataDestination
        costClass = value.costClass
    }
}

/// A view-safe projection. It deliberately omits bindings, authorization snapshots,
/// body hashes, raw responses, paths and retry internals.
public enum ProcessingArtifactPresentationPayload: Equatable, Sendable {
    case draftChunk(EnsembleDocument)
    case draft(EnsembleDraftDocument)
    case synthesis(EnsembleDocument)
}

public struct ProcessingArtifactPresentation: Equatable, Sendable, Identifiable {
    public let artifactID: String
    public let kind: ProcessingArtifactKind
    public let reused: Bool
    public let createdAt: String
    public let generation: ProcessingArtifactGenerationPresentation?
    public let payload: ProcessingArtifactPresentationPayload
    public var id: String { artifactID }

    public init?(_ response: ProcessingArtifactResponse) {
        switch response.payload {
        case .draftChunk(let document): payload = .draftChunk(document)
        case .draft(let document): payload = .draft(document)
        case .synthesis(let document): payload = .synthesis(document)
        case .sourceIndex, .source: return nil
        }
        artifactID = response.artifact.artifactID
        kind = response.artifact.kind
        reused = response.reused
        createdAt = response.artifact.createdAt
        generation = response.artifact.generation.map(ProcessingArtifactGenerationPresentation.init)
    }
}

@MainActor
@Observable
public final class ProcessingRunHistoryStore {
    public private(set) var runs: [ProcessingRunSummary] = []
    public private(set) var nextCursor: String?
    public private(set) var selectedRunID: String?
    public private(set) var run: ProcessingRun?
    public private(set) var artifacts: [String: ProcessingArtifactPresentation] = [:]
    public private(set) var artifactFailures: Set<String> = []
    public private(set) var loadingArtifactIDs: Set<String> = []
    public private(set) var isLoadingList = false
    public private(set) var isLoadingRun = false
    public private(set) var listErrorMessage: String?
    public private(set) var runErrorMessage: String?

    @ObservationIgnored private let client: any ProcessingRunHistoryClient
    @ObservationIgnored private var context: Context?
    @ObservationIgnored private var contextRevision: UInt64 = 0
    @ObservationIgnored private var listRevision: UInt64 = 0
    @ObservationIgnored private var selectionRevision: UInt64 = 0
    @ObservationIgnored private var artifactRevisions: [String: UInt64] = [:]

    private struct Context: Equatable {
        let meetingID: String
        let scope: String?
        let connectionGeneration: UInt64
    }

    public init(client: any ProcessingRunHistoryClient) {
        self.client = client
    }

    public func refresh(
        meetingID: String, scope: String?, connectionGeneration: UInt64
    ) async {
        let expected = Context(
            meetingID: meetingID, scope: scope, connectionGeneration: connectionGeneration)
        adopt(expected)
        guard !isLoadingList else { return }
        listRevision &+= 1
        let token = (contextRevision, listRevision)
        isLoadingList = true
        listErrorMessage = nil
        do {
            let response = try await client.listProcessingRuns(
                meetingID: meetingID, scope: scope, limit: 100, cursor: nil,
                connectionGeneration: connectionGeneration)
            guard current(expected, contextRevision: token.0), listRevision == token.1,
                response.meetingID == meetingID else { return }
            let priorSelection = selectedRunID.flatMap { id in runs.first { $0.runID == id } }
            runs = Self.deduplicated(response.runs)
            if let priorSelection, !runs.contains(where: { $0.runID == priorSelection.runID }) {
                runs.append(priorSelection)
            }
            nextCursor = response.nextCursor
            if selectedRunID == nil { selectWithoutLoading(runs.first?.runID) }
            finishList(token)
            await refreshSelectedRun()
        } catch {
            guard current(expected, contextRevision: token.0), listRevision == token.1 else { return }
            listErrorMessage = "生成履歴を読み込めません。接続と会議の scope を確認してください。"
            finishList(token)
        }
    }

    public func loadMore() async {
        guard let context, let cursor = nextCursor, !isLoadingList else { return }
        listRevision &+= 1
        let token = (contextRevision, listRevision)
        isLoadingList = true
        listErrorMessage = nil
        do {
            let response = try await client.listProcessingRuns(
                meetingID: context.meetingID, scope: context.scope, limit: 100, cursor: cursor,
                connectionGeneration: context.connectionGeneration)
            guard current(context, contextRevision: token.0), listRevision == token.1,
                response.meetingID == context.meetingID else { return }
            var known = Set(runs.map(\.runID))
            runs += response.runs.filter { known.insert($0.runID).inserted }
            nextCursor = response.nextCursor
            finishList(token)
        } catch {
            guard current(context, contextRevision: token.0), listRevision == token.1 else { return }
            listErrorMessage = "続きの生成履歴を読み込めません。接続と会議の scope を確認してください。"
            finishList(token)
        }
    }

    public func selectRun(_ runID: String?) async {
        if let runID, !ProcessingIdentifier.run(runID) { return }
        if selectedRunID != runID { selectWithoutLoading(runID) }
        await refreshSelectedRun()
    }

    public func refreshSelectedRun() async {
        guard let context, let runID = selectedRunID, !isLoadingRun else { return }
        selectionRevision &+= 1
        let token = (contextRevision, selectionRevision, runID)
        isLoadingRun = true
        runErrorMessage = nil
        do {
            let response = try await client.getProcessingRun(
                meetingID: context.meetingID, scope: context.scope, runID: runID,
                connectionGeneration: context.connectionGeneration)
            guard current(context, contextRevision: token.0), selectionRevision == token.1,
                selectedRunID == token.2, response.meetingID == context.meetingID,
                response.run.runID == runID else { return }
            run = response.run
            finishRun(token)
            var seenDrafts = Set<String>()
            for slot in response.run.canonicalSlots {
                guard let artifactID = slot.draftArtifactID,
                    seenDrafts.insert(artifactID).inserted else { continue }
                await loadArtifact(artifactID, expectedKind: .draft)
            }
            if let artifactID = response.run.synthesisArtifactID {
                await loadArtifact(artifactID, expectedKind: .synthesis)
            }
        } catch {
            guard current(context, contextRevision: token.0), selectionRevision == token.1,
                selectedRunID == token.2 else { return }
            runErrorMessage = "生成runの詳細を読み込めません。状態を再確認してください。"
            finishRun(token)
        }
    }

    public func loadDraftContents(artifactID: String) async {
        guard ProcessingIdentifier.artifact(artifactID) else { return }
        await loadArtifact(artifactID, expectedKind: .draft)
        guard case .draft(let draft)? = artifacts[artifactID]?.payload else { return }
        var seen = Set<String>()
        for part in draft.parts where seen.insert(part.documentArtifactID).inserted {
            await loadArtifact(part.documentArtifactID, expectedKind: .draftChunk)
        }
    }

    public func draftDocuments(artifactID: String) -> [EnsembleDocument] {
        draftDocumentArtifacts(artifactID: artifactID).compactMap { artifact in
            guard case .draftChunk(let document) = artifact.payload else { return nil }
            return document
        }
    }

    public func draftDocumentArtifacts(artifactID: String) -> [ProcessingArtifactPresentation] {
        guard case .draft(let draft)? = artifacts[artifactID]?.payload else { return [] }
        var seen = Set<String>()
        return draft.parts.compactMap { part in
            guard seen.insert(part.documentArtifactID).inserted,
                let artifact = artifacts[part.documentArtifactID],
                case .draftChunk = artifact.payload else { return nil }
            return artifact
        }
    }

    public func missingDraftDocumentCount(artifactID: String) -> Int {
        draftDocumentIDs(artifactID: artifactID).filter { artifacts[$0] == nil }.count
    }

    public func loadingDraftDocumentCount(artifactID: String) -> Int {
        draftDocumentIDs(artifactID: artifactID).filter(loadingArtifactIDs.contains).count
    }

    public func failedDraftDocumentCount(artifactID: String) -> Int {
        draftDocumentIDs(artifactID: artifactID).filter(artifactFailures.contains).count
    }

    public func invalidate() {
        context = nil
        contextRevision &+= 1
        clearAll()
    }

    private func adopt(_ next: Context) {
        guard context != next else { return }
        context = next
        contextRevision &+= 1
        clearAll()
    }

    private func clearAll() {
        listRevision &+= 1
        selectionRevision &+= 1
        artifactRevisions.removeAll()
        runs = []
        nextCursor = nil
        selectedRunID = nil
        clearSelection()
        isLoadingList = false
        listErrorMessage = nil
    }

    private func selectWithoutLoading(_ runID: String?) {
        selectedRunID = runID
        selectionRevision &+= 1
        clearSelection()
    }

    private func clearSelection() {
        run = nil
        artifacts = [:]
        artifactFailures = []
        loadingArtifactIDs = []
        isLoadingRun = false
        runErrorMessage = nil
    }

    private func loadArtifact(_ artifactID: String, expectedKind: ProcessingArtifactKind) async {
        guard let context, let runID = selectedRunID, artifacts[artifactID] == nil,
            !loadingArtifactIDs.contains(artifactID) else { return }
        let revision = (artifactRevisions[artifactID] ?? 0) &+ 1
        artifactRevisions[artifactID] = revision
        let token = (contextRevision, selectionRevision, revision, runID)
        loadingArtifactIDs.insert(artifactID)
        artifactFailures.remove(artifactID)
        do {
            let response = try await client.getProcessingArtifact(
                meetingID: context.meetingID, scope: context.scope, runID: runID,
                artifactID: artifactID, connectionGeneration: context.connectionGeneration)
            guard current(context, contextRevision: token.0), selectionRevision == token.1,
                artifactRevisions[artifactID] == token.2, selectedRunID == token.3 else { return }
            guard response.requestedRunID == runID,
                response.artifact.artifactID == artifactID,
                response.artifact.kind == expectedKind else {
                artifactFailures.insert(artifactID)
                loadingArtifactIDs.remove(artifactID)
                return
            }
            guard let presentation = ProcessingArtifactPresentation(response) else {
                artifactFailures.insert(artifactID)
                loadingArtifactIDs.remove(artifactID)
                return
            }
            artifacts[artifactID] = presentation
            loadingArtifactIDs.remove(artifactID)
        } catch {
            guard current(context, contextRevision: token.0), selectionRevision == token.1,
                artifactRevisions[artifactID] == token.2, selectedRunID == token.3 else { return }
            artifactFailures.insert(artifactID)
            loadingArtifactIDs.remove(artifactID)
        }
    }

    private func current(_ expected: Context, contextRevision: UInt64) -> Bool {
        !Task.isCancelled && context == expected && self.contextRevision == contextRevision
    }

    private static func deduplicated(_ values: [ProcessingRunSummary]) -> [ProcessingRunSummary] {
        var seen = Set<String>()
        return values.filter { seen.insert($0.runID).inserted }
    }

    private func draftDocumentIDs(artifactID: String) -> Set<String> {
        guard case .draft(let draft)? = artifacts[artifactID]?.payload else { return [] }
        return Set(draft.parts.map(\.documentArtifactID))
    }

    private func finishList(_ token: (UInt64, UInt64)) {
        if contextRevision == token.0, listRevision == token.1 { isLoadingList = false }
    }

    private func finishRun(_ token: (UInt64, UInt64, String)) {
        if contextRevision == token.0, selectionRevision == token.1,
            selectedRunID == token.2 { isLoadingRun = false }
    }
}
