import XCTest
@testable import NarumiMenuBarCore

final class ProcessingArtifactModelsTests: XCTestCase {
    func testFormalArtifactExamplesDecodeWithKindBoundPayloads() throws {
        let values = try ContractExampleFixture.outputs(tool: "get_processing_artifact")
            .map { try JSONDecoder().decode(ProcessingArtifactResponse.self, from: $0) }
        XCTAssertFalse(values.isEmpty)
        XCTAssertTrue(values.allSatisfy(\.isWellFormed))
        XCTAssertTrue(values.contains { if case .synthesis = $0.payload { true } else { false } })
    }

    func testResponseRejectsUnknownFieldsAndCrossRunOrReuseMismatch() throws {
        let data = try XCTUnwrap(ContractExampleFixture.outputs(tool: "get_processing_artifact").first)
        var object = try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
        object["secret"] = "must not decode"
        XCTAssertThrowsError(try JSONDecoder().decode(
            ProcessingArtifactResponse.self, from: JSONSerialization.data(withJSONObject: object)))

        object.removeValue(forKey: "secret")
        object["requested_run_id"] = "run-99999999999999999999999999999999"
        XCTAssertThrowsError(try JSONDecoder().decode(
            ProcessingArtifactResponse.self, from: JSONSerialization.data(withJSONObject: object)))

        object = try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
        object["reused"] = !((object["reused"] as? Bool) ?? false)
        XCTAssertThrowsError(try JSONDecoder().decode(
            ProcessingArtifactResponse.self, from: JSONSerialization.data(withJSONObject: object)))
    }

    func testMissingUsageStaysUnknownAndIsNotInventedAsZero() throws {
        let usage = try JSONDecoder().decode(
            ProcessingGenerationUsage.self, from: Data(#"{"output_tokens":12}"#.utf8))
        XCTAssertNil(usage.inputTokens)
        XCTAssertEqual(usage.outputTokens, 12)
        XCTAssertThrowsError(try JSONDecoder().decode(
            ProcessingGenerationUsage.self, from: Data(#"{}"#.utf8)))
    }

    func testCodexGenerationUsesOpenAIDestination() throws {
        let data = Data(#"""
        {
          "requested_selection":{"provider":"codex-app-server","connection_id":"conn-111122223333","connection_revision":1,"model_id":"fixture-text-model","parameters":{"reasoning_effort":"high"},"cache_epoch":0},
          "effective_parameters":{"reasoning_effort":"high"},"returned_model":null,"usage":null,
          "data_destination":"openai","cost_class":"subscription","retry_lineage":null
        }
        """#.utf8)
        XCTAssertEqual(try JSONDecoder().decode(
            ProcessingGenerationMetadata.self, from: data).dataDestination, .openai)
        var object = try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
        object["data_destination"] = "local"
        XCTAssertThrowsError(try JSONDecoder().decode(
            ProcessingGenerationMetadata.self, from: JSONSerialization.data(withJSONObject: object)))
    }

    func testNestedDocumentsRequireNullableFieldsAndRejectUnknownKeys() throws {
        let outputs = try ContractExampleFixture.outputs(tool: "get_processing_artifact")
        var source = try XCTUnwrap(JSONSerialization.jsonObject(with: outputs[1]) as? [String: Any])
        var payload = try XCTUnwrap(source["payload"] as? [String: Any])
        var evidence = try XCTUnwrap(payload["evidence"] as? [[String: Any]])
        evidence[0].removeValue(forKey: "speaker_label")
        payload["evidence"] = evidence; source["payload"] = payload
        XCTAssertThrowsError(try JSONDecoder().decode(
            ProcessingArtifactResponse.self, from: JSONSerialization.data(withJSONObject: source)))

        var synthesis = try XCTUnwrap(JSONSerialization.jsonObject(with: outputs[0]) as? [String: Any])
        payload = try XCTUnwrap(synthesis["payload"] as? [String: Any])
        var claims = try XCTUnwrap(payload["claims"] as? [[String: Any]])
        claims[0]["secret"] = "must not decode"
        payload["claims"] = claims; synthesis["payload"] = payload
        XCTAssertThrowsError(try JSONDecoder().decode(
            ProcessingArtifactResponse.self, from: JSONSerialization.data(withJSONObject: synthesis)))

        synthesis = try XCTUnwrap(JSONSerialization.jsonObject(with: outputs[0]) as? [String: Any])
        payload = try XCTUnwrap(synthesis["payload"] as? [String: Any])
        claims = try XCTUnwrap(payload["claims"] as? [[String: Any]])
        claims[0]["owner"] = "担当者"
        payload["claims"] = claims; synthesis["payload"] = payload
        XCTAssertThrowsError(try JSONDecoder().decode(
            ProcessingArtifactResponse.self, from: JSONSerialization.data(withJSONObject: synthesis)))

        synthesis = try XCTUnwrap(JSONSerialization.jsonObject(with: outputs[0]) as? [String: Any])
        payload = try XCTUnwrap(synthesis["payload"] as? [String: Any])
        claims = try XCTUnwrap(payload["claims"] as? [[String: Any]])
        let evidenceRefs = try XCTUnwrap(claims[0]["evidence"] as? [[String: Any]])
        payload["questions"] = [[
            "id": "qu_" + String(repeating: "1", count: 64), "kind": "conflict", "text": "どちらを採用するか",
            "alternatives": [["text": "案1", "evidence": evidenceRefs]],
        ]]
        synthesis["payload"] = payload
        XCTAssertThrowsError(try JSONDecoder().decode(
            ProcessingArtifactResponse.self, from: JSONSerialization.data(withJSONObject: synthesis)))
    }
}
