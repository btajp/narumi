import Foundation
import XCTest
@testable import NarumiMenuBarCore

final class TranscriptionModelSelectionTests: XCTestCase {
    private var object: [String: Any] {
        ["provider": "openai-api", "connection_id": ProviderSettingsFixtures.connectionID,
         "connection_revision": 1, "model_id": "whisper-1"]
    }

    private func decode(_ object: [String: Any]) throws -> TranscriptionModelSelection {
        try JSONDecoder().decode(
            TranscriptionModelSelection.self, from: JSONSerialization.data(withJSONObject: object))
    }

    func testBothModelsRoundTripWithEmptyParametersAndDefaultEpoch() throws {
        for modelID in TranscriptionModelSelection.modelIDs {
            var source = object
            source["model_id"] = modelID
            let selection = try decode(source)
            XCTAssertTrue(selection.isWellFormed)
            XCTAssertEqual(selection.cacheEpoch, 0)
            XCTAssertEqual(selection.parameters, .init())
            let data = try JSONEncoder().encode(selection)
            let encoded = try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
            XCTAssertEqual(encoded["provider"] as? String, "openai-api")
            XCTAssertEqual(encoded["model_id"] as? String, modelID)
            XCTAssertEqual(encoded["cache_epoch"] as? Int, 0)
            XCTAssertEqual((encoded["parameters"] as? [String: Any])?.count, 0)
            XCTAssertEqual(try JSONDecoder().decode(TranscriptionModelSelection.self, from: data), selection)
        }
    }

    func testCustomOptionsAndNonObjectParametersAreRejected() throws {
        let rejected: [Any] = [
            NSNull(), [], "", 0, true,
            ["prompt": "fixture-private-text"], ["language": "ja"], ["speaker_names": ["Fixture"]],
            ["known_speaker_references": ["fixture"]], ["response_format": "json"],
            ["reasoning_effort": "high"], ["max_tokens": 4096], ["store": false],
            ["api_key": "fixture-private-key"],
        ]
        for parameters in rejected {
            var source = object
            source["parameters"] = parameters
            XCTAssertThrowsError(try decode(source)) { error in
                XCTAssertFalse(String(describing: error).contains("fixture-private"))
            }
        }
    }

    func testUnknownSelectionFieldsAndMissingRequiredFieldsAreRejected() throws {
        for key in ["provider", "connection_id", "connection_revision", "model_id"] {
            var missing = object
            missing.removeValue(forKey: key)
            XCTAssertThrowsError(try decode(missing))
        }
        for key in ["language", "endpoint", "transcription_retry", "input_fingerprint"] {
            var extra = object
            extra[key] = "fixture-extra-value"
            XCTAssertThrowsError(try decode(extra))
        }
    }

    func testUnsupportedProviderModelAndInvalidRevisionsCannotDecode() throws {
        let invalid: [(String, Any)] = [
            ("provider", "codex-app-server"), ("provider", "anthropic-api"), ("provider", "ollama"),
            ("model_id", "gpt-4o-transcribe"), ("model_id", "gpt-4o-mini-transcribe"),
            ("model_id", "whisper-1-custom"), ("connection_id", "conn-bad"),
            ("connection_id", "conn-0123456789AB"), ("connection_id", "conn-0123456789ab\n"),
            ("connection_revision", 0), ("connection_revision", -1), ("connection_revision", true),
            ("connection_revision", "1"), ("cache_epoch", -1), ("cache_epoch", true),
            ("cache_epoch", NSNull()), ("cache_epoch", "0"),
        ]
        for (key, value) in invalid {
            var source = object
            source[key] = value
            XCTAssertThrowsError(try decode(source), key)
        }
    }

    func testMutableEpochRemainsValidatedWithoutAddingRetryAuthorization() throws {
        var selection = try decode(object)
        selection.cacheEpoch = 7
        let encoded = try XCTUnwrap(
            JSONSerialization.jsonObject(with: JSONEncoder().encode(selection)) as? [String: Any])
        XCTAssertEqual(encoded["cache_epoch"] as? Int, 7)
        XCTAssertNil(encoded["transcription_retry"])
        XCTAssertNil(encoded["input_fingerprint"])
        selection.cacheEpoch = -1
        XCTAssertFalse(selection.isWellFormed)
        XCTAssertThrowsError(try JSONEncoder().encode(selection))
    }

    func testProgrammaticInvalidSelectionCannotBeEncoded() {
        for selection in [
            TranscriptionModelSelection(provider: "codex-app-server", connectionID: ProviderSettingsFixtures.connectionID,
                                        connectionRevision: 1, modelID: "whisper-1"),
            TranscriptionModelSelection(connectionID: ProviderSettingsFixtures.connectionID,
                                        connectionRevision: 1, modelID: "gpt-4o-transcribe"),
            TranscriptionModelSelection(connectionID: ProviderSettingsFixtures.connectionID,
                                        connectionRevision: 0, modelID: "whisper-1"),
        ] {
            XCTAssertFalse(selection.isWellFormed)
            XCTAssertThrowsError(try JSONEncoder().encode(selection))
        }
    }

    func testSwiftLanguageIdentifiersMatchPythonCanonicalSet() throws {
        var root = URL(fileURLWithPath: #filePath)
        for _ in 0..<4 { root.deleteLastPathComponent() }
        let source = try String(contentsOf: root.appendingPathComponent("pipeline/src/narumi/transcription_selection.py"), encoding: .utf8)
        let definition = try NSRegularExpression(pattern: #"(?s)ISO_639_1_LANGUAGES\s*:[^=]*=\s*frozenset\((.*?)\n\)"#)
        let match = try XCTUnwrap(definition.firstMatch(in: source, range: NSRange(source.startIndex..., in: source)))
        let body = (source as NSString).substring(with: match.range(at: 1))
        let strings = try NSRegularExpression(pattern: "\"([a-z ]+)\"")
        let matches = strings.matches(in: body, range: NSRange(body.startIndex..., in: body))
        let languages = matches.flatMap {
            (body as NSString).substring(with: $0.range(at: 1)).split(separator: " ").map { String($0) }
        }
        let expected = Set(languages)
        XCTAssertFalse(expected.isEmpty)
        XCTAssertEqual(TranscriptionModelForm.iso6391Languages, expected)
        XCTAssertFalse(expected.contains("auto"))
        XCTAssertTrue(TranscriptionModelForm.isSupportedLanguage("auto"))
    }

    func testLanguageValidationDoesNotNormalizeOrGuessIdentifiers() {
        for language in ["auto", "ja", "en", "zh", "fr", "tl", "he"] {
            XCTAssertTrue(TranscriptionModelForm.isSupportedLanguage(language), language)
        }
        for language in ["", "AUTO", "JA", "ja-JP", "en_US", "eng", "auto ", " ja", "xx", "zz", "sh", "mo", "iw"] {
            XCTAssertFalse(TranscriptionModelForm.isSupportedLanguage(language), language)
        }
    }
}
