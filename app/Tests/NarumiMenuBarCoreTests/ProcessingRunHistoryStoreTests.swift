import XCTest
@testable import NarumiMenuBarCore

@MainActor
final class ProcessingRunHistoryStoreTests: XCTestCase {
    private let meetingA = "20260827T030500Z-a1b2c3d4"
    private let meetingB = "20260828T030500Z-b1c2d3e4"
    private let runA = "run-33333333333333333333333333333333"
    private let runB = "run-44444444444444444444444444444444"
    private let draftID = "artifact-cccccccccccccccccccccccccccccccc"
    private let documentID = "artifact-dddddddddddddddddddddddddddddddd"
    private let sourceID = "artifact-eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"

    func testHistoryCapabilityRequiresContractSixAndPublishedLimits() {
        let supported = ServerCapabilities(
            recording: true, transports: ["streamable-http"], transcriptionEngines: [],
            diarizationEngines: [], llmProviders: [], exportDestinations: [],
            minutesEnsembleLimits: MinutesEnsembleLimits())
        XCTAssertTrue(supported.supportsProcessingRunHistory(contractVersion: "6.0.0"))
        XCTAssertFalse(supported.supportsProcessingRunHistory(contractVersion: "5.0.0"))

        let noLimits = ServerCapabilities(
            recording: true, transports: ["streamable-http"], transcriptionEngines: [],
            diarizationEngines: [], llmProviders: [], exportDestinations: [])
        XCTAssertFalse(noLimits.supportsProcessingRunHistory(contractVersion: "6.0.0"))
    }

    func testRefreshLoadsCurrentDraftRootOnly() async throws {
        let fixtures = try fixtures()
        let client = FakeProcessingRunHistoryClient(
            lists: [meetingA: fixtures.listA], runs: [runA: fixtures.runA],
            artifacts: [draftID: fixtures.draft])
        let store = ProcessingRunHistoryStore(client: client)

        await store.refresh(meetingID: meetingA, scope: "cloudnative", connectionGeneration: 3)

        XCTAssertEqual(store.selectedRunID, runA)
        XCTAssertEqual(store.run?.runID, runA)
        XCTAssertEqual(store.artifacts[draftID]?.kind, .draft)
        let requests = await client.artifactRequestIDs()
        XCTAssertEqual(requests, [draftID])
    }

    func testDraftExpansionLoadsDocumentsButNeverSourceArtifacts() async throws {
        let fixtures = try fixtures()
        let client = FakeProcessingRunHistoryClient(
            lists: [meetingA: fixtures.listA], runs: [runA: fixtures.runA],
            artifacts: [draftID: fixtures.draft, documentID: fixtures.draftChunk])
        let store = ProcessingRunHistoryStore(client: client)
        await store.refresh(meetingID: meetingA, scope: nil, connectionGeneration: 1)

        await store.loadDraftContents(artifactID: draftID)

        let requests = await client.artifactRequestIDs()
        XCTAssertEqual(requests, [draftID, documentID])
        XCTAssertFalse(requests.contains(sourceID))
        XCTAssertEqual(store.draftDocuments(artifactID: draftID).count, 1)
        XCTAssertEqual(store.missingDraftDocumentCount(artifactID: draftID), 0)
    }

    func testFailedDraftDocumentStopsLoadingAndRemainsExplicitlyRetryable() async throws {
        let fixtures = try fixtures()
        let client = FakeProcessingRunHistoryClient(
            lists: [meetingA: fixtures.listA], runs: [runA: fixtures.runA],
            artifacts: [draftID: fixtures.draft])
        let store = ProcessingRunHistoryStore(client: client)
        await store.refresh(meetingID: meetingA, scope: nil, connectionGeneration: 1)

        await store.loadDraftContents(artifactID: draftID)

        XCTAssertEqual(store.loadingDraftDocumentCount(artifactID: draftID), 0)
        XCTAssertEqual(store.failedDraftDocumentCount(artifactID: draftID), 1)
        XCTAssertEqual(store.missingDraftDocumentCount(artifactID: draftID), 1)
    }

    func testDelayedOldMeetingListCannotOverwriteNewConnectionContext() async throws {
        let fixtures = try fixtures()
        let client = FakeProcessingRunHistoryClient(
            lists: [meetingA: fixtures.listA, meetingB: fixtures.listB],
            runs: [runA: fixtures.runA, runB: fixtures.runB], artifacts: [:],
            listDelays: [meetingA: .milliseconds(140)])
        let store = ProcessingRunHistoryStore(client: client)

        let old = Task { await store.refresh(meetingID: meetingA, scope: nil, connectionGeneration: 10) }
        try await Task.sleep(for: .milliseconds(20))
        await store.refresh(meetingID: meetingB, scope: "next", connectionGeneration: 11)
        await old.value

        XCTAssertEqual(store.runs.map(\.runID), [runB])
        XCTAssertEqual(store.selectedRunID, runB)
        XCTAssertEqual(store.run?.runID, runB)
    }

    func testDelayedSelectedRunCannotReplaceNewMeetingRun() async throws {
        let fixtures = try fixtures()
        let client = FakeProcessingRunHistoryClient(
            lists: [meetingA: fixtures.listA, meetingB: fixtures.listB],
            runs: [runA: fixtures.runA, runB: fixtures.runB], artifacts: [:],
            runDelays: [runA: .milliseconds(140)])
        let store = ProcessingRunHistoryStore(client: client)

        let old = Task { await store.refresh(meetingID: meetingA, scope: nil, connectionGeneration: 20) }
        try await Task.sleep(for: .milliseconds(20))
        await store.refresh(meetingID: meetingB, scope: nil, connectionGeneration: 21)
        await old.value

        XCTAssertEqual(store.selectedRunID, runB)
        XCTAssertEqual(store.run?.runID, runB)
    }

    func testDelayedOldConnectionGenerationCannotOverwriteCurrentGeneration() async throws {
        let fixtures = try fixtures()
        let client = FakeProcessingRunHistoryClient(
            lists: [meetingA: fixtures.listA], runs: [runA: fixtures.runA, runB: fixtures.runBInA],
            artifacts: [:], generationLists: [30: fixtures.listA, 31: fixtures.listBA],
            generationListDelays: [30: .milliseconds(140)])
        let store = ProcessingRunHistoryStore(client: client)

        let old = Task { await store.refresh(meetingID: meetingA, scope: nil, connectionGeneration: 30) }
        try await Task.sleep(for: .milliseconds(20))
        await store.refresh(meetingID: meetingA, scope: nil, connectionGeneration: 31)
        await old.value

        XCTAssertEqual(store.runs.map(\.runID), [runB, runA])
        XCTAssertEqual(store.selectedRunID, runB)
        XCTAssertEqual(store.run?.runID, runB)
    }

    func testDelayedRunCannotOverwriteNewSelectionInSameMeeting() async throws {
        let fixtures = try fixtures()
        let client = FakeProcessingRunHistoryClient(
            lists: [meetingA: fixtures.listAB], runs: [runA: fixtures.runA, runB: fixtures.runBInA],
            artifacts: [:], runDelays: [runA: .milliseconds(140)])
        let store = ProcessingRunHistoryStore(client: client)

        let old = Task { await store.refresh(meetingID: meetingA, scope: nil, connectionGeneration: 40) }
        try await Task.sleep(for: .milliseconds(20))
        await store.selectRun(runB)
        await old.value

        XCTAssertEqual(store.selectedRunID, runB)
        XCTAssertEqual(store.run?.runID, runB)
    }

    func testArtifactProjectionOmitsPrivateResponseFields() throws {
        let formal = try XCTUnwrap(ContractExampleFixture.outputs(tool: "get_processing_artifact").first)
        let response = try JSONDecoder().decode(ProcessingArtifactResponse.self, from: formal)
        let value = try XCTUnwrap(ProcessingArtifactPresentation(response))

        XCTAssertEqual(Set(Mirror(reflecting: value).children.compactMap(\.label)), [
            "artifactID", "kind", "reused", "createdAt", "generation", "payload",
        ])
        let generation = try XCTUnwrap(value.generation)
        XCTAssertEqual(Set(Mirror(reflecting: generation).children.compactMap(\.label)), [
            "provider", "connectionID", "connectionRevision", "modelID", "cacheEpoch",
            "effectiveParameters", "returnedModel", "usage", "dataDestination", "costClass",
        ])
        let sourceData = try XCTUnwrap(ContractExampleFixture.outputs(tool: "get_processing_artifact").last)
        let source = try JSONDecoder().decode(ProcessingArtifactResponse.self, from: sourceData)
        XCTAssertNil(ProcessingArtifactPresentation(source), "source evidence must not enter the UI projection")
    }

    func testFailuresUseFixedMessagesAndMismatchedKindStopsLoading() async throws {
        let fixtures = try fixtures()
        let client = FakeProcessingRunHistoryClient(
            lists: [meetingA: fixtures.listA], runs: [runA: fixtures.runA],
            artifacts: [draftID: fixtures.synthesis])
        let store = ProcessingRunHistoryStore(client: client)

        await store.refresh(meetingID: meetingA, scope: nil, connectionGeneration: 1)

        XCTAssertTrue(store.artifactFailures.contains(draftID))
        XCTAssertFalse(store.loadingArtifactIDs.contains(draftID))

        let failing = FakeProcessingRunHistoryClient(
            lists: [:], runs: [:], artifacts: [:],
            listErrors: [meetingA: SensitiveFixtureError("sk-secret /Users/private/raw.json")])
        let failingStore = ProcessingRunHistoryStore(client: failing)
        await failingStore.refresh(meetingID: meetingA, scope: nil, connectionGeneration: 1)
        let message = try XCTUnwrap(failingStore.listErrorMessage)
        XCTAssertFalse(message.contains("sk-secret"))
        XCTAssertFalse(message.contains("/Users/private"))
    }

    private struct FixtureValues {
        let listA: ListProcessingRunsResponse
        let listB: ListProcessingRunsResponse
        let listAB: ListProcessingRunsResponse
        let listBA: ListProcessingRunsResponse
        let runA: GetProcessingRunResponse
        let runB: GetProcessingRunResponse
        let runBInA: GetProcessingRunResponse
        let draft: ProcessingArtifactResponse
        let draftChunk: ProcessingArtifactResponse
        let synthesis: ProcessingArtifactResponse
    }

    private func fixtures() throws -> FixtureValues {
        let listData = try XCTUnwrap(ContractExampleFixture.outputs(tool: "list_processing_runs").first)
        let runData = try XCTUnwrap(ContractExampleFixture.outputs(tool: "get_processing_run").first)
        let artifactData = try XCTUnwrap(ContractExampleFixture.outputs(tool: "get_processing_artifact").first)
        let listBData = replacing(listData, [meetingA: meetingB, runA: runB])
        let runBData = replacing(runData, [meetingA: meetingB, runA: runB])
        let runBInAData = replacing(runData, [runA: runB])
        let synthesis = try JSONDecoder().decode(ProcessingArtifactResponse.self, from: artifactData)
        return try FixtureValues(
            listA: JSONDecoder().decode(ListProcessingRunsResponse.self, from: listData),
            listB: JSONDecoder().decode(ListProcessingRunsResponse.self, from: listBData),
            listAB: makeList(from: listData, runIDs: [runA, runB]),
            listBA: makeList(from: listData, runIDs: [runB, runA]),
            runA: JSONDecoder().decode(GetProcessingRunResponse.self, from: runData),
            runB: JSONDecoder().decode(GetProcessingRunResponse.self, from: runBData),
            runBInA: JSONDecoder().decode(GetProcessingRunResponse.self, from: runBInAData),
            draft: makeDraft(from: artifactData),
            draftChunk: makeDraftChunk(from: artifactData),
            synthesis: synthesis)
    }

    private func makeList(from data: Data, runIDs: [String]) throws -> ListProcessingRunsResponse {
        var root = try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
        let original = try XCTUnwrap((root["runs"] as? [[String: Any]])?.first)
        root["runs"] = runIDs.map { runID -> [String: Any] in
            var summary = original
            summary["run_id"] = runID
            return summary
        }
        return try JSONDecoder().decode(
            ListProcessingRunsResponse.self,
            from: JSONSerialization.data(withJSONObject: root))
    }

    private func makeDraft(from synthesis: Data) throws -> ProcessingArtifactResponse {
        var root = try XCTUnwrap(JSONSerialization.jsonObject(with: synthesis) as? [String: Any])
        var artifact = try XCTUnwrap(root["artifact"] as? [String: Any])
        artifact["artifact_id"] = draftID
        artifact["node_id"] = NSNull()
        artifact["kind"] = "draft"
        artifact["body_sha256"] = String(repeating: "1", count: 64)
        artifact["source_artifact_ids"] = [sourceID]
        artifact["origin"] = NSNull()
        artifact["generation"] = NSNull()
        root["artifact"] = artifact
        root["payload"] = [
            "schema_version": "ensemble-draft-v1",
            "parts": [["source_artifact_id": sourceID, "document_artifact_id": documentID]],
        ]
        var binding = try XCTUnwrap(root["binding"] as? [String: Any])
        binding["artifact_id"] = draftID
        binding["dependency_mappings"] = []
        binding["retry_lineage"] = NSNull()
        root["binding"] = binding
        root["reused"] = false
        return try JSONDecoder().decode(
            ProcessingArtifactResponse.self,
            from: JSONSerialization.data(withJSONObject: root))
    }

    private func makeDraftChunk(from synthesis: Data) throws -> ProcessingArtifactResponse {
        var root = try XCTUnwrap(JSONSerialization.jsonObject(with: synthesis) as? [String: Any])
        var artifact = try XCTUnwrap(root["artifact"] as? [String: Any])
        artifact["artifact_id"] = documentID
        artifact["kind"] = "draft_chunk"
        artifact["body_sha256"] = String(repeating: "2", count: 64)
        root["artifact"] = artifact
        var binding = try XCTUnwrap(root["binding"] as? [String: Any])
        binding["artifact_id"] = documentID
        root["binding"] = binding
        return try JSONDecoder().decode(
            ProcessingArtifactResponse.self,
            from: JSONSerialization.data(withJSONObject: root))
    }

    private func replacing(_ data: Data, _ values: [String: String]) -> Data {
        var text = String(decoding: data, as: UTF8.self)
        for (old, new) in values { text = text.replacingOccurrences(of: old, with: new) }
        return Data(text.utf8)
    }
}

private struct SensitiveFixtureError: Error, Sendable {
    let value: String
    init(_ value: String) { self.value = value }
}

private actor FakeProcessingRunHistoryClient: ProcessingRunHistoryClient {
    let lists: [String: ListProcessingRunsResponse]
    let runs: [String: GetProcessingRunResponse]
    let artifacts: [String: ProcessingArtifactResponse]
    let listDelays: [String: Duration]
    let runDelays: [String: Duration]
    let generationLists: [UInt64: ListProcessingRunsResponse]
    let generationListDelays: [UInt64: Duration]
    let listErrors: [String: SensitiveFixtureError]
    private var artifactRequests: [String] = []

    init(
        lists: [String: ListProcessingRunsResponse], runs: [String: GetProcessingRunResponse],
        artifacts: [String: ProcessingArtifactResponse],
        listDelays: [String: Duration] = [:], runDelays: [String: Duration] = [:],
        generationLists: [UInt64: ListProcessingRunsResponse] = [:],
        generationListDelays: [UInt64: Duration] = [:],
        listErrors: [String: SensitiveFixtureError] = [:]
    ) {
        self.lists = lists; self.runs = runs; self.artifacts = artifacts
        self.listDelays = listDelays; self.runDelays = runDelays; self.listErrors = listErrors
        self.generationLists = generationLists; self.generationListDelays = generationListDelays
    }

    func listProcessingRuns(
        meetingID: String, scope _: String?, limit _: Int, cursor _: String?,
        connectionGeneration: UInt64
    ) async throws -> ListProcessingRunsResponse {
        if let delay = generationListDelays[connectionGeneration] { try await Task.sleep(for: delay) }
        if let delay = listDelays[meetingID] { try await Task.sleep(for: delay) }
        if let error = listErrors[meetingID] { throw error }
        if let value = generationLists[connectionGeneration] { return value }
        guard let value = lists[meetingID] else { throw SensitiveFixtureError("missing list") }
        return value
    }

    func getProcessingRun(
        meetingID _: String, scope _: String?, runID: String,
        connectionGeneration _: UInt64
    ) async throws -> GetProcessingRunResponse {
        if let delay = runDelays[runID] { try await Task.sleep(for: delay) }
        guard let value = runs[runID] else { throw SensitiveFixtureError("missing run") }
        return value
    }

    func getProcessingArtifact(
        meetingID _: String, scope _: String?, runID _: String, artifactID: String,
        connectionGeneration _: UInt64
    ) async throws -> ProcessingArtifactResponse {
        artifactRequests.append(artifactID)
        guard let value = artifacts[artifactID] else { throw SensitiveFixtureError("missing artifact") }
        return value
    }

    func artifactRequestIDs() -> [String] { artifactRequests }
}
