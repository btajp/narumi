import Foundation
import XCTest
@testable import NarumiMenuBarCore

final class TranscriptionRequestRecoveryTests: XCTestCase {
    func testKeepsExactOriginalBytesAndPinsTheConfirmedIdentityConfigAndRetry() throws {
        var object = try RecoveryRecordFixture.object()
        object["scope"] = "会議の範囲"
        object["reason"] = "引用 \"one\" /区切り\\改行\nタブ\t\u{08}\u{0C}\r"
        var config = try RecoveryRecordFixture.configuration()
        config["self_name"] = "話者"
        config["vocab_hints"] = ["{\"same\":1,\"same\":2}", "e\u{301}", "引用 \"と\\"]
        object["expected_config"] = config
        let json = try RecoveryRecordFixture.text(object)
            .replacingOccurrences(of: "\"request_id\"", with: #""request\u005fid""#)
        var data = Data((" \n\t" + json + "\r\n").utf8)
        let original = data
        let record = try recover(data)
        data.append(32)
        XCTAssertEqual(record.arguments, original)
        XCTAssertNotEqual(record.arguments, data)
        XCTAssertEqual(record.id, RecoveryRecordFixture.requestID)
        XCTAssertEqual(record.requestID, RecoveryRecordFixture.requestID)
        XCTAssertEqual(record.meetingID, TranscriptionRetryFixtures.meetingID)
        XCTAssertEqual(record.scope, "会議の範囲")
        XCTAssertEqual(record.expectedConfig.selfName, "話者")
        XCTAssertEqual(record.expectedConfig.vocabHints, config["vocab_hints"] as? [String])
        XCTAssertEqual(record.expectedConfig.minutesModel, try TranscriptionRetryFixtures.config(epoch: 1).minutesModel)
        XCTAssertEqual(record.expectedConfig.transcriptionModel?.cacheEpoch, 1)
        XCTAssertEqual(record.transcriptionRetry, try TranscriptionRetryFixtures.details().retry)
        XCTAssertEqual(record, try recover(original))
    }

    func testPermittedOmissionsAndExplicitNullableConfigFieldsAreNotWrittenBack() throws {
        for useNull in [false, true] {
            var object = try RecoveryRecordFixture.object()
            object.removeValue(forKey: "force")
            var config = try RecoveryRecordFixture.configuration()
            config.removeValue(forKey: "self_name")
            config.removeValue(forKey: "minutes_model")
            if useNull {
                config["self_name"] = NSNull()
                config["minutes_model"] = NSNull()
            }
            var selection = try RecoveryRecordFixture.selection()
            selection.removeValue(forKey: "parameters")
            config["transcription_model"] = selection
            object["expected_config"] = config
            let data = try RecoveryRecordFixture.data(object)
            let record = try recover(data)
            XCTAssertNil(record.scope)
            XCTAssertNil(record.expectedConfig.selfName)
            XCTAssertNil(record.expectedConfig.minutesModel)
            XCTAssertEqual(record.arguments, data)
        }
    }

    func testBothAudioModelsAndEverySupportedMinutesProviderRemainPinned() throws {
        for model in TranscriptionModelSelection.modelIDs {
            for provider in MinutesModelSelection.providers {
                var object = try RecoveryRecordFixture.object()
                var config = try RecoveryRecordFixture.configuration()
                var selection = try RecoveryRecordFixture.selection()
                selection["model_id"] = model
                config["transcription_model"] = selection
                var minutes = try RecoveryRecordFixture.minutes()
                minutes["provider"] = provider
                let parameters: [String: Any] = provider == "codex-app-server"
                    ? ["reasoning_effort": "high"] : ["max_tokens": 4096]
                minutes["parameters"] = parameters
                config["minutes_model"] = minutes
                config["language"] = "auto"
                object["expected_config"] = config
                let record = try recover(object)
                XCTAssertEqual(record.expectedConfig.minutesModel?.provider, provider)
                XCTAssertEqual(record.expectedConfig.transcriptionModel?.modelID, model)
                XCTAssertEqual(record.expectedConfig.language, "auto")
            }
        }
    }

    func testLargeIntegerEpochAndRevisionAreNotRoundedThroughFloatingPoint() throws {
        var object = try RecoveryRecordFixture.object()
        var config = try RecoveryRecordFixture.configuration()
        var selection = try RecoveryRecordFixture.selection()
        selection["cache_epoch"] = Int.max
        selection["connection_revision"] = 9_007_199_254_740_993
        config["transcription_model"] = selection
        object["expected_config"] = config
        var retry = RecoveryRecordFixture.retry()
        retry["blocked_epoch"] = Int.max - 1
        object["transcription_retry"] = retry
        let record = try recover(object)
        XCTAssertEqual(record.expectedConfig.transcriptionModel?.cacheEpoch, Int.max)
        XCTAssertEqual(record.expectedConfig.transcriptionModel?.connectionRevision, 9_007_199_254_740_993)
        XCTAssertEqual(record.transcriptionRetry.blockedEpoch, Int.max - 1)
    }

    func testRequestToolAndOriginalRequestIDMustMatch() throws {
        let data = try RecoveryRecordFixture.data(RecoveryRecordFixture.object())
        for tool in ["", "process", "export_minutes", "Regenerate", "regenerate "] {
            assertInvalid(data, tool: tool)
        }
        assertInvalid(data, requestID: "different-request-0001")
        for requestID in ["", "1234567", String(repeating: "r", count: 129)] {
            var object = try RecoveryRecordFixture.object()
            object["request_id"] = requestID
            assertInvalid(try RecoveryRecordFixture.data(object), requestID: requestID)
        }
        var canonicallyEquivalent = try RecoveryRecordFixture.object()
        canonicallyEquivalent["request_id"] = "request-é"
        assertInvalid(try RecoveryRecordFixture.data(canonicallyEquivalent), requestID: "request-e\u{301}")
    }

    func testMissingNullAndWronglyTypedRequiredArgumentsAreRejected() throws {
        let invalidValues: [Any] = [NSNull(), true, 1, [], "fixture-private-request-body"]
        for field in ["request_id", "meeting_id", "expected_config", "transcription_retry"] {
            var object = try RecoveryRecordFixture.object()
            object.removeValue(forKey: field)
            try assertInvalid(object)
            for invalid in invalidValues {
                object[field] = invalid
                try assertInvalid(object)
            }
        }
        for meetingID in ["", "other", TranscriptionRetryFixtures.meetingID + "\n", "20260829T000000Z-A1B2C3D4"] {
            var object = try RecoveryRecordFixture.object()
            object["meeting_id"] = meetingID
            try assertInvalid(object)
        }
    }

    func testUnknownTopLevelFieldsAndUnsupportedStagesAreRejected() throws {
        let updates: [[String: Any]] = [
            ["stages": ["transcribe"]], ["api_key": "fixture-private-request-body"],
            ["arguments": [:]], ["retry": true], ["scope_name": "other"],
        ]
        for update in updates {
            var object = try RecoveryRecordFixture.object()
            object.merge(update) { _, new in new }
            try assertInvalid(object)
        }
    }

    func testForceMustBeBooleanFalseAndOptionalReasonMustBeBoundedText() throws {
        let invalidForce: [Any] = [true, NSNull(), "false", 0, 1, [], [:]]
        let invalidReason: [Any] = [NSNull(), "", String(repeating: "x", count: 501), true, 1, [], [:]]
        for (field, values) in [("force", invalidForce), ("reason", invalidReason)] {
            for value in values {
                var object = try RecoveryRecordFixture.object()
                object[field] = value
                try assertInvalid(object)
            }
        }
    }

    func testScopeRejectsNullArraysEmptyNamesAndOverlongNames() throws {
        let invalid: [Any] = [NSNull(), "", String(repeating: "s", count: 65), [], ["single"], ["a", "b"], true, 1]
        for value in invalid {
            var object = try RecoveryRecordFixture.object()
            object["scope"] = value
            try assertInvalid(object)
        }
    }

    func testStringBoundsCountUnicodeScalarsNotGraphemeClusters() throws {
        let requestID = String(repeating: "e\u{301}", count: 64)
        var object = try RecoveryRecordFixture.object()
        object["request_id"] = requestID
        object["scope"] = String(repeating: "e\u{301}", count: 32)
        object["reason"] = String(repeating: "e\u{301}", count: 250)
        XCTAssertEqual(try recover(RecoveryRecordFixture.data(object), requestID: requestID).requestID, requestID)
        for field in ["scope", "reason", "request_id"] {
            var invalid = object
            invalid[field] = (object[field] as? String ?? "") + "a"
            assertInvalid(
                try RecoveryRecordFixture.data(invalid),
                requestID: field == "request_id" ? requestID + "a" : requestID)
        }
    }

    func testRecoveryRequiresTheFullEffectiveConfiguration() throws {
        let required = [
            "transcription_engine", "diarization_engine", "llm_provider", "external_send_policy",
            "language", "vocab_hints", "transcription_model",
        ]
        for field in required {
            var config = try RecoveryRecordFixture.configuration()
            config.removeValue(forKey: field)
            try assertInvalidConfiguration(config)
            config[field] = NSNull()
            try assertInvalidConfiguration(config)
        }
    }

    func testConfigDoesNotCoerceTypesOrIgnoreAdditionalFields() throws {
        let updates: [[String: Any]] = [
            ["transcription_engine": 1], ["diarization_engine": []], ["llm_provider": true],
            ["language": 1], ["self_name": ["name"]], ["vocab_hints": "word"],
            ["vocab_hints": ["word", 1]], ["vocab_hints": [NSNull()]],
            ["stages": []], ["transcription_retry": RecoveryRecordFixture.retry()],
            ["credentials": "fixture-private-request-body"],
        ]
        for update in updates {
            var config = try RecoveryRecordFixture.configuration()
            config.merge(update) { _, new in new }
            try assertInvalidConfiguration(config)
        }
        for policy in ["local_only", "subscription_ok", "unknown", "API_OK"] {
            var config = try RecoveryRecordFixture.configuration()
            config["external_send_policy"] = policy
            try assertInvalidConfiguration(config)
        }
        for language in ["", "zz", "EN", "ja-JP", "en\n", "a", "jａ"] {
            var config = try RecoveryRecordFixture.configuration()
            config["language"] = language
            try assertInvalidConfiguration(config)
        }
    }

    func testTranscriptionSelectionRejectsMissingIdentityInvalidValuesAndCustomParameters() throws {
        for field in ["provider", "connection_id", "connection_revision", "model_id"] {
            var selection = try RecoveryRecordFixture.selection()
            selection.removeValue(forKey: field)
            try assertInvalidSelection(selection)
            selection[field] = NSNull()
            try assertInvalidSelection(selection)
        }
        let updates: [[String: Any]] = [
            ["provider": "anthropic-api"], ["model_id": "gpt-4o-transcribe"],
            ["connection_id": "conn-not-valid"], ["connection_id": TranscriptionRetryFixtures.connectionID + "\n"],
            ["connection_revision": 0], ["connection_revision": true], ["connection_revision": 1.5],
            ["connection_revision": "3"], ["cache_epoch": -1], ["cache_epoch": true],
            ["cache_epoch": "1"], ["cache_epoch": NSNull()], ["parameters": NSNull()],
            ["parameters": []], ["parameters": ["prompt": "fixture-private-request-body"]],
            ["parameters": ["max_tokens": 100]], ["runtime": [:]],
        ]
        for update in updates {
            var selection = try RecoveryRecordFixture.selection()
            selection.merge(update) { _, new in new }
            try assertInvalidSelection(selection)
        }
    }

    func testEpochMustBeStrictlyAboveTheBlockedEpochWithoutImplicitIncrement() throws {
        for epoch in [0, 1] {
            var object = try RecoveryRecordFixture.object()
            var config = try RecoveryRecordFixture.configuration()
            var selection = try RecoveryRecordFixture.selection()
            selection["cache_epoch"] = epoch
            config["transcription_model"] = selection
            object["expected_config"] = config
            var retry = RecoveryRecordFixture.retry()
            retry["blocked_epoch"] = 1
            object["transcription_retry"] = retry
            try assertInvalid(object)
        }
        var selection = try RecoveryRecordFixture.selection()
        selection.removeValue(forKey: "cache_epoch")
        try assertInvalidSelection(selection)
    }

    func testRetryMustContainOnlyTheThreeTypedProofFields() throws {
        for field in ["input_fingerprint", "chunk_fingerprint", "blocked_epoch"] {
            var retry = RecoveryRecordFixture.retry()
            retry.removeValue(forKey: field)
            try assertInvalidRetry(retry)
            retry[field] = NSNull()
            try assertInvalidRetry(retry)
        }
        let updates: [[String: Any]] = [
            ["blocked_epoch": true], ["blocked_epoch": -1], ["blocked_epoch": 0.5], ["blocked_epoch": "0"],
            ["input_fingerprint": String(repeating: "A", count: 64)], ["chunk_fingerprint": "short"],
            ["chunk_fingerprint": TranscriptionRetryFixtures.chunkFingerprint + "\n"],
            ["force": false], ["transcript": "fixture-private-request-body"],
        ]
        for update in updates {
            var retry = RecoveryRecordFixture.retry()
            retry.merge(update) { _, new in new }
            try assertInvalidRetry(retry)
        }
    }

    func testInvalidMinutesSelectionCannotBeDiscardedFromTheConfirmedConfig() throws {
        let updates: [[String: Any]] = [
            ["provider": "claude-agent-sdk"], ["model_id": ""], ["connection_revision": true],
            ["provider": "codex-app-server", "parameters": ["max_tokens": 1]],
            ["provider": "anthropic-api", "parameters": ["reasoning_effort": "high"]],
            ["parameters": ["reasoning_effort": NSNull()]], ["parameters": ["max_tokens": 0]],
            ["parameters": ["max_tokens": 32769]], ["parameters": ["max_tokens": true]],
            ["parameters": ["api_key": "fixture-private-request-body"]],
            ["model_id": String(repeating: "e\u{301}", count: 129)], ["unknown": "value"],
        ]
        for update in updates {
            var config = try RecoveryRecordFixture.configuration()
            var minutes = try RecoveryRecordFixture.minutes()
            minutes.merge(update) { _, new in new }
            config["minutes_model"] = minutes
            try assertInvalidConfiguration(config)
        }
    }

    func testDuplicateKeysAreRejectedAtEveryObjectDepthIncludingEscapedSpellings() throws {
        let original = try RecoveryRecordFixture.text(RecoveryRecordFixture.object())
        let pairs: [(String, String)] = [
            (#""force":false"#, #""force":false,"force":false"#),
            (#""request_id":"recovery-request-0001""#,
             #""request_id":"recovery-request-0001","request\u005fid":"recovery-request-0001""#),
            (#""language":"ja""#, #""language":"ja","language":"en""#),
            (#""cache_epoch":1"#, #""cache_epoch":1,"cache_epoch":2"#),
            (#""parameters":{}"#, #""parameters":{},"parameters":{}"#),
            (#""max_tokens":2048"#, #""max_tokens":2048,"max_tokens":4096"#),
            (#""blocked_epoch":0"#, #""blocked_epoch":0,"blocked\u005fepoch":0"#),
        ]
        for (needle, replacement) in pairs {
            XCTAssertTrue(original.contains(needle), needle)
            assertInvalid(Data(original.replacingOccurrences(of: needle, with: replacement).utf8))
        }
    }

    func testMalformedJSONAndInvalidUTF8OnlyProduceTheFixedSafeError() throws {
        let valid = try RecoveryRecordFixture.text(RecoveryRecordFixture.object())
        for text in ["", "[]", "null", "true", valid + "{}", "/*comment*/" + valid,
                     String(valid.dropLast()) + ",}", "{\"request_id\":\"unterminated", "{\"x\":\"\\uD800\"}"] {
            assertInvalid(Data(text.utf8))
        }
        assertInvalid(Data([123, 34, 120, 34, 58, 34, 255, 34, 125]))
        assertInvalid(Data((String(repeating: "[", count: 65) + "0" + String(repeating: "]", count: 65)).utf8))
    }

    private func recover(
        _ data: Data, requestID: String = RecoveryRecordFixture.requestID, tool: String = ToolCatalog.regenerate
    ) throws -> TranscriptionRequestRecovery {
        try TranscriptionRequestRecovery(request: .init(requestID: requestID, tool: tool, arguments: data))
    }

    private func recover(_ object: [String: Any]) throws -> TranscriptionRequestRecovery {
        try recover(RecoveryRecordFixture.data(object))
    }

    private func assertInvalid(
        _ data: Data, requestID: String = RecoveryRecordFixture.requestID, tool: String = ToolCatalog.regenerate,
        file: StaticString = #filePath, line: UInt = #line
    ) {
        XCTAssertThrowsError(try recover(data, requestID: requestID, tool: tool), file: file, line: line) { error in
            XCTAssertEqual(error as? TranscriptionRequestRecoveryError, .invalidRequest, file: file, line: line)
            XCTAssertEqual(error.localizedDescription, TranscriptionRequestRecoveryError.invalidRequest.errorDescription ?? "")
            XCTAssertFalse(String(reflecting: error).contains("fixture-private-request-body"), file: file, line: line)
        }
    }

    private func assertInvalid(_ object: [String: Any], file: StaticString = #filePath, line: UInt = #line) throws {
        assertInvalid(try RecoveryRecordFixture.data(object), file: file, line: line)
    }

    private func assertInvalidConfiguration(_ config: [String: Any], file: StaticString = #filePath, line: UInt = #line) throws {
        var object = try RecoveryRecordFixture.object()
        object["expected_config"] = config
        try assertInvalid(object, file: file, line: line)
    }

    private func assertInvalidSelection(_ selection: [String: Any], file: StaticString = #filePath, line: UInt = #line) throws {
        var config = try RecoveryRecordFixture.configuration()
        config["transcription_model"] = selection
        try assertInvalidConfiguration(config, file: file, line: line)
    }

    private func assertInvalidRetry(_ retry: [String: Any], file: StaticString = #filePath, line: UInt = #line) throws {
        var object = try RecoveryRecordFixture.object()
        object["transcription_retry"] = retry
        try assertInvalid(object, file: file, line: line)
    }
}

private enum RecoveryRecordFixture {
    static let requestID = "recovery-request-0001"

    static func object() throws -> [String: Any] {
        ["request_id": requestID, "meeting_id": TranscriptionRetryFixtures.meetingID, "force": false,
         "expected_config": try configuration(), "transcription_retry": retry()]
    }

    static func configuration() throws -> [String: Any] {
        try XCTUnwrap(JSONSerialization.jsonObject(
            with: JSONEncoder().encode(TranscriptionRetryFixtures.config(epoch: 1))) as? [String: Any])
    }

    static func selection() throws -> [String: Any] {
        try XCTUnwrap(configuration()["transcription_model"] as? [String: Any])
    }

    static func minutes() throws -> [String: Any] {
        try XCTUnwrap(configuration()["minutes_model"] as? [String: Any])
    }

    static func retry() -> [String: Any] {
        ["input_fingerprint": TranscriptionRetryFixtures.inputFingerprint,
         "chunk_fingerprint": TranscriptionRetryFixtures.chunkFingerprint, "blocked_epoch": 0]
    }

    static func data(_ object: [String: Any]) throws -> Data {
        try JSONSerialization.data(withJSONObject: object, options: [.sortedKeys])
    }

    static func text(_ object: [String: Any]) throws -> String {
        try String(decoding: data(object), as: UTF8.self)
    }
}
