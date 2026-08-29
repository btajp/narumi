import XCTest
@testable import NarumiMenuBarCore

final class ProcessingRunModelsTests: XCTestCase {
    private let runA = "run-11111111111111111111111111111111"
    private let runB = "run-22222222222222222222222222222222"

    func testFormalRunExamplesDecode() throws {
        let summaries = try ContractExampleFixture.outputs(tool: "list_processing_runs")
            .map { try JSONDecoder().decode(ListProcessingRunsResponse.self, from: $0) }
        XCTAssertFalse(summaries.isEmpty)
        XCTAssertTrue(summaries.flatMap(\.runs).allSatisfy(\.isWellFormed))
        let runs = try ContractExampleFixture.outputs(tool: "get_processing_run")
            .map { try JSONDecoder().decode(GetProcessingRunResponse.self, from: $0) }
        XCTAssertFalse(runs.isEmpty)
        XCTAssertTrue(runs.allSatisfy { $0.run.isWellFormed })
    }

    func testRunRejectsInconsistentNodeStateAndDraftBinding() throws {
        let data = try XCTUnwrap(ContractExampleFixture.outputs(tool: "get_processing_run").first)
        var object = try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
        var run = try XCTUnwrap(object["run"] as? [String: Any])
        var nodes = try XCTUnwrap(run["nodes"] as? [[String: Any]])
        nodes[0]["status"] = "reused"
        nodes[0]["reused"] = false
        nodes[0]["artifact_id"] = NSNull()
        run["nodes"] = nodes; object["run"] = run
        XCTAssertThrowsError(try JSONDecoder().decode(
            GetProcessingRunResponse.self, from: JSONSerialization.data(withJSONObject: object)))

        object = try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
        run = try XCTUnwrap(object["run"] as? [String: Any])
        var slots = try XCTUnwrap(run["canonical_slots"] as? [[String: Any]])
        slots[0]["draft_artifact_id"] = "artifact-99999999999999999999999999999999"
        run["canonical_slots"] = slots; object["run"] = run
        XCTAssertThrowsError(try JSONDecoder().decode(
            GetProcessingRunResponse.self, from: JSONSerialization.data(withJSONObject: object)))

        object = try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
        run = try XCTUnwrap(object["run"] as? [String: Any])
        slots = try XCTUnwrap(run["canonical_slots"] as? [[String: Any]])
        run["canonical_slots"] = Array(slots.reversed()); object["run"] = run
        XCTAssertThrowsError(try JSONDecoder().decode(
            GetProcessingRunResponse.self, from: JSONSerialization.data(withJSONObject: object)))
    }

    func testUnknownNodeMayRetainAnOriginFromAnOlderRun() throws {
        let data = try XCTUnwrap(ContractExampleFixture.outputs(tool: "get_processing_run").first)
        var object = try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
        var run = try XCTUnwrap(object["run"] as? [String: Any])
        var nodes = try XCTUnwrap(run["nodes"] as? [[String: Any]])
        var origin = try XCTUnwrap(nodes[1]["origin"] as? [String: Any])
        origin["run_id"] = runB; nodes[1]["origin"] = origin
        var blocked = try XCTUnwrap(run["blocked"] as? [[String: Any]])
        blocked[0]["origin"] = origin
        run["nodes"] = nodes; run["blocked"] = blocked; object["run"] = run
        XCTAssertNoThrow(try JSONDecoder().decode(
            GetProcessingRunResponse.self, from: JSONSerialization.data(withJSONObject: object)))
    }

    func testRunListRequiresNullableCursorAndAcceptsOpaqueCursor() throws {
        let data = try XCTUnwrap(ContractExampleFixture.outputs(tool: "list_processing_runs").first)
        var object = try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
        object.removeValue(forKey: "next_cursor")
        XCTAssertThrowsError(try JSONDecoder().decode(
            ListProcessingRunsResponse.self, from: JSONSerialization.data(withJSONObject: object)))
        object["next_cursor"] = "opaque_page_2"
        XCTAssertEqual(try JSONDecoder().decode(
            ListProcessingRunsResponse.self, from: JSONSerialization.data(withJSONObject: object)).nextCursor,
            "opaque_page_2")
    }

    func testContractSixRequiresNullableRunKeysWhileFiveAllowsTheirAbsence() throws {
        let legacy = Data(#"{"job_id":"job-0123456789ab","kind":"process","status":"running","created_at":"2026-08-29T00:00:00Z","updated_at":"2026-08-29T00:00:01Z"}"#.utf8)
        let job = try JSONDecoder().decode(Job.self, from: legacy)
        XCTAssertTrue(job.validatesProcessingRunCorrelation(contractVersion: "5.0.0"))
        XCTAssertFalse(job.validatesProcessingRunCorrelation(contractVersion: "6.0.0"))

        let current = Data(#"{"job_id":"job-0123456789ab","kind":"process","status":"running","processing_run_id":null,"created_at":"2026-08-29T00:00:00Z","updated_at":"2026-08-29T00:00:01Z"}"#.utf8)
        XCTAssertTrue(try JSONDecoder().decode(Job.self, from: current)
            .validatesProcessingRunCorrelation(contractVersion: "6.0.0"))
    }

    func testSucceededResultMustCarryAndMatchTopLevelRun() throws {
        func decode(
            kind: String = "regenerate", top: String?, result: String?, stages: [String]? = nil
        ) throws -> Job {
            func nullable(_ value: String?) -> Any { value.map { $0 as Any } ?? NSNull() }
            var resultObject: [String: Any] = ["processing_run_id": nullable(result)]
            if let stages { resultObject["stages"] = stages }
            let object: [String: Any] = [
                "job_id": "job-0123456789ab", "kind": kind, "status": "succeeded",
                "processing_run_id": nullable(top), "result": resultObject,
                "created_at": "2026-08-29T00:00:00Z", "updated_at": "2026-08-29T00:00:01Z",
            ]
            return try JSONDecoder().decode(Job.self, from: JSONSerialization.data(withJSONObject: object))
        }
        XCTAssertTrue(try decode(top: runA, result: runA).validatesProcessingRunCorrelation(contractVersion: "6.0.0"))
        XCTAssertFalse(try decode(top: runA, result: runB).validatesProcessingRunCorrelation(contractVersion: "6.0.0"))
        XCTAssertFalse(try decode(top: nil, result: runA).validatesProcessingRunCorrelation(contractVersion: "6.0.0"))
        XCTAssertFalse(try decode(top: nil, result: nil, stages: ["minutes_ensemble"])
            .validatesProcessingRunCorrelation(contractVersion: "6.0.0"))
        XCTAssertTrue(try decode(top: runA, result: runA, stages: ["minutes_ensemble"])
            .validatesProcessingRunCorrelation(contractVersion: "6.0.0"))
        XCTAssertFalse(try decode(kind: "export", top: runA, result: runA)
            .validatesProcessingRunCorrelation(contractVersion: "6.0.0"))
        XCTAssertFalse(try decode(kind: "provider_setup", top: runA, result: runA)
            .validatesProcessingRunCorrelation(contractVersion: "6.0.0"))
    }

    func testDesktopRejectsKnownRunRegressionAndReplacement() throws {
        func job(_ run: String?) -> Job {
            Job(jobID: "job", kind: "process", status: "running", processingRunID: run,
                createdAt: "2026-08-29T00:00:00Z", updatedAt: "2026-08-29T00:00:01Z")
        }
        for replacement in [nil, runB] as [String?] {
            var state = DesktopJobState(); state.track(jobID: "job")
            XCTAssertTrue(state.finishRefresh(try XCTUnwrap(state.beginRefresh()), jobs: [job(runA)]))
            let before = state
            XCTAssertFalse(state.finishRefresh(try XCTUnwrap(state.beginRefresh()), jobs: [job(replacement)]))
            XCTAssertEqual(state.jobs, before.jobs)
        }
    }

    func testMinutesRetryIsClosedAndHonorsBothAttemptLimits() throws {
        let target = ProcessingTargetRef(
            runID: runA, nodeID: "node-" + String(repeating: "1", count: 64),
            callID: "call-" + String(repeating: "2", count: 64))
        let origin = ProcessingOriginRef(
            runID: target.runID, nodeID: target.nodeID, callID: target.callID,
            attemptID: "attempt-" + String(repeating: "3", count: 32), provider: "openai-api",
            connectionID: "conn-111122223333", connectionRevision: 1, modelID: "fixture")
        let current = ProcessingCurrentSelection(
            provider: "openai-api", connectionID: "conn-111122223333", connectionRevision: 1, modelID: "fixture")
        let details = MinutesOutcomeUnknownDetails(
            target: target, blockedAttemptID: origin.attemptID, origin: origin, currentSelection: current,
            contentFingerprint: String(repeating: "a", count: 64), latestAttemptOutcome: .unknown,
            attemptsUsed: 1, retryAttemptsUsed: 1)
        XCTAssertTrue(try MinutesRetry(details: details).isWellFormed)
        var object = try XCTUnwrap(JSONSerialization.jsonObject(
            with: JSONEncoder().encode(try MinutesRetry(details: details))) as? [String: Any])
        object["cache_epoch"] = 1
        XCTAssertThrowsError(try JSONDecoder().decode(
            MinutesRetry.self, from: JSONSerialization.data(withJSONObject: object)))
        let exhausted = MinutesOutcomeUnknownDetails(
            target: target, blockedAttemptID: origin.attemptID, origin: origin, currentSelection: current,
            contentFingerprint: String(repeating: "a", count: 64), latestAttemptOutcome: .unknown,
            attemptsUsed: 64, retryAttemptsUsed: 1)
        XCTAssertThrowsError(try MinutesRetry(details: exhausted))
    }
}
