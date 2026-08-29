import Foundation
import XCTest
@testable import NarumiMenuBarCore

final class TranscriptionRetryTests: XCTestCase {
    private let requiredFields: Set<String> = [
        "stage", "reason", "outcome_unknown", "input_fingerprint", "chunk_fingerprint", "blocked_epoch",
        "track", "chunk_index", "chunk_count", "completed_chunks", "start_sample", "end_sample", "sample_rate",
    ]

    func testAllThirteenRequiredFieldsRoundTripWithoutInventingOptionalProvenance() throws {
        let details = try TranscriptionRetryFixtures.details()
        let encoded = try JSONEncoder().encode(details)
        let object = try XCTUnwrap(JSONSerialization.jsonObject(with: encoded) as? [String: Any])
        XCTAssertEqual(Set(object.keys), requiredFields)
        XCTAssertEqual(try JSONDecoder().decode(TranscriptionOutcomeUnknownDetails.self, from: encoded), details)
        XCTAssertNil(details.provider)
        XCTAssertNil(details.modelID)
        XCTAssertNil(details.connectionID)
        XCTAssertNil(details.connectionRevision)
        XCTAssertEqual(details.completedChunks, 1)
        XCTAssertEqual(details.startSeconds, 0)
        XCTAssertEqual(details.endSeconds, 600)
    }

    func testOptionalSelectedModelProvenanceRoundTripsOnlyWhenSupplied() throws {
        let details = try TranscriptionRetryFixtures.details([
            "provider": "openai-api", "model_id": "gpt-4o-transcribe-diarize",
            "connection_id": TranscriptionRetryFixtures.connectionID, "connection_revision": 7,
        ])
        let data = try JSONEncoder().encode(details)
        XCTAssertEqual(try JSONDecoder().decode(TranscriptionOutcomeUnknownDetails.self, from: data), details)
        XCTAssertEqual(details.provider, "openai-api")
        XCTAssertEqual(details.modelID, "gpt-4o-transcribe-diarize")
        XCTAssertEqual(details.connectionRevision, 7)
    }

    func testMissingOrNullRequiredEvidenceNeverEnablesRetry() throws {
        for field in requiredFields {
            var object = TranscriptionRetryFixtures.detailsObject()
            object.removeValue(forKey: field)
            XCTAssertThrowsError(try decode(object), "missing \(field)")
            object[field] = NSNull()
            XCTAssertThrowsError(try decode(object), "null \(field)")
        }
        for field in ["provider", "model_id", "connection_id", "connection_revision"] {
            XCTAssertThrowsError(try TranscriptionRetryFixtures.details([field: NSNull()]), "null \(field)")
        }
    }

    func testEvidenceConstantsAndClosedShapeRejectOtherOutcomesAndSecretFields() throws {
        let updates: [[String: Any]] = [
            ["stage": "generate"], ["reason": "provider_generation_outcome_unknown"],
            ["outcome_unknown": false], ["outcome_unknown": "true"], ["outcome_unknown": 1],
            ["track": "screen"], ["sample_rate": 48_000], ["sample_rate": "16000"],
            ["provider": "anthropic-api"], ["model_id": "gpt-4o-transcribe"],
            ["connection_id": "conn-not-a-contract-id"], ["connection_revision": 0],
            ["upstream_body": "fixture-private-upstream-body"],
        ]
        for update in updates {
            XCTAssertThrowsError(try TranscriptionRetryFixtures.details(update)) { error in
                XCTAssertFalse(String(reflecting: error).contains("fixture-private-upstream-body"))
            }
        }
    }

    func testFingerprintValidationRejectsMalformedOrNonCanonicalHashes() throws {
        for value in [
            "", String(repeating: "a", count: 63), String(repeating: "a", count: 65),
            String(repeating: "A", count: 64), String(repeating: "g", count: 64),
            String(repeating: "a", count: 64) + "\n", String(repeating: "あ", count: 64),
        ] {
            for key in ["input_fingerprint", "chunk_fingerprint"] {
                XCTAssertThrowsError(try TranscriptionRetryFixtures.details([key: value]))
            }
            XCTAssertThrowsError(try TranscriptionRetry(
                inputFingerprint: value, chunkFingerprint: TranscriptionRetryFixtures.chunkFingerprint, blockedEpoch: 0))
        }
    }

    func testEvidenceRejectsCrossFieldInconsistencyAndOutOfBoundsIntegers() throws {
        let updates: [[String: Any]] = [
            ["blocked_epoch": -1], ["blocked_epoch": true], ["blocked_epoch": 0.5],
            ["chunk_index": -1], ["chunk_index": 3], ["chunk_index": 144, "chunk_count": 144],
            ["chunk_count": 0], ["chunk_count": 145], ["completed_chunks": -1], ["completed_chunks": 4],
            ["start_sample": -1], ["start_sample": 1_382_400_000], ["end_sample": 0],
            ["end_sample": 1_382_400_001], ["start_sample": 16_000, "end_sample": 16_000],
            ["start_sample": 16_001, "end_sample": 16_000], ["end_sample": 9_600_001],
            ["start_sample": 1_382_399_999, "end_sample": 1_382_399_998],
        ]
        for update in updates {
            XCTAssertThrowsError(try TranscriptionRetryFixtures.details(update))
        }
    }

    func testEvidenceAcceptsContractBoundaryAndTrackRelativeHalfOpenRange() throws {
        let edge = try TranscriptionRetryFixtures.details([
            "blocked_epoch": Int.max, "chunk_index": 143, "chunk_count": 144, "completed_chunks": 144,
            "start_sample": 1_382_399_999, "end_sample": 1_382_400_000,
        ])
        XCTAssertEqual(edge.endSample - edge.startSample, 1)
        let relative = try TranscriptionRetryFixtures.details(["track": "mic", "start_sample": 16_000, "end_sample": 32_000])
        XCTAssertEqual(relative.startSeconds, 1)
        XCTAssertEqual(relative.endSeconds, 2)
    }

    func testRetrySerializesOnlyTheConfirmedInputChunkAndBlockedEpoch() throws {
        let details = try TranscriptionRetryFixtures.details(["blocked_epoch": 6])
        let retry = details.retry
        let data = try JSONEncoder().encode(retry)
        let object = try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
        XCTAssertEqual(Set(object.keys), ["input_fingerprint", "chunk_fingerprint", "blocked_epoch"])
        XCTAssertEqual(object["blocked_epoch"] as? Int, 6)
        XCTAssertEqual(retry.inputFingerprint, details.inputFingerprint)
        XCTAssertEqual(retry.chunkFingerprint, details.chunkFingerprint)
        XCTAssertTrue(retry.isWellFormed)
        XCTAssertEqual(try JSONDecoder().decode(TranscriptionRetry.self, from: data), retry)
    }

    func testEpochAloneOrExtraRetryFieldsCannotAuthorizeResending() throws {
        let objects: [[String: Any]] = [
            ["cache_epoch": 1], ["blocked_epoch": 1],
            ["input_fingerprint": TranscriptionRetryFixtures.inputFingerprint, "blocked_epoch": 1],
            ["input_fingerprint": TranscriptionRetryFixtures.inputFingerprint,
             "chunk_fingerprint": TranscriptionRetryFixtures.chunkFingerprint, "blocked_epoch": -1],
            ["input_fingerprint": TranscriptionRetryFixtures.inputFingerprint,
             "chunk_fingerprint": TranscriptionRetryFixtures.chunkFingerprint, "blocked_epoch": 1, "force": true],
        ]
        for object in objects {
            XCTAssertThrowsError(try JSONDecoder().decode(
                TranscriptionRetry.self, from: JSONSerialization.data(withJSONObject: object)))
        }
        var object: [String: Any] = [
            "input_fingerprint": TranscriptionRetryFixtures.inputFingerprint,
            "chunk_fingerprint": TranscriptionRetryFixtures.chunkFingerprint, "blocked_epoch": 0,
        ]
        for field in ["input_fingerprint", "chunk_fingerprint", "blocked_epoch"] {
            let previous = object.removeValue(forKey: field)
            XCTAssertThrowsError(try JSONDecoder().decode(
                TranscriptionRetry.self, from: JSONSerialization.data(withJSONObject: object)))
            object[field] = NSNull()
            XCTAssertThrowsError(try JSONDecoder().decode(
                TranscriptionRetry.self, from: JSONSerialization.data(withJSONObject: object)))
            object[field] = previous
        }
    }

    private func decode(_ object: [String: Any]) throws -> TranscriptionOutcomeUnknownDetails {
        try JSONDecoder().decode(
            TranscriptionOutcomeUnknownDetails.self, from: JSONSerialization.data(withJSONObject: object))
    }
}
