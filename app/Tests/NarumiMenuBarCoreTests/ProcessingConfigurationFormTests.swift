import XCTest
@testable import NarumiMenuBarCore

final class ProcessingConfigurationFormTests: XCTestCase {
    func testEditingKeepsOriginalConfigAndMeetingScopeForCAS() throws {
        var config = MeetingConfig.serverDefaults
        config.externalSendPolicy = "api_ok"
        config.transcriptionModel = selection()
        config.minutesModel = MinutesModelFixtures.selection()
        var detail = try MinutesModelFixtures.detail(config: config)
        detail.meeting.scope = "original"
        var form = MeetingConfigurationForm(detail: detail)
        form.processing.language = "en"
        form.processing.vocabHintsText = "narumi\nAPI"
        form.scopeText = "changed"

        XCTAssertEqual(form.meetingID, detail.meeting.meetingID)
        XCTAssertEqual(form.originalScope, "original")
        XCTAssertEqual(form.processing.originalConfig, config)
        let expected = try form.processing.makeUpdate().applying(to: form.processing.originalConfig)
        XCTAssertEqual(expected.language, "en")
        XCTAssertEqual(expected.vocabHints, ["narumi", "API"])
        XCTAssertEqual(expected.transcriptionModel, config.transcriptionModel)
        XCTAssertEqual(expected.minutesModel, config.minutesModel)
        XCTAssertNotEqual(expected, form.processing.originalConfig)
    }

    func testSparseFieldsRetainValuesWhileExplicitNullClearsOverrides() throws {
        var original = MeetingConfig.serverDefaults
        original.transcriptionEngine = "mlx-whisper"
        original.language = "en"
        original.externalSendPolicy = "api_ok"
        original.selfName = "Fixture"
        original.vocabHints = ["original"]
        original.transcriptionModel = selection()
        original.minutesModel = MinutesModelFixtures.selection()
        var form = ProcessingConfigurationForm(config: original)
        form.transcriptionEngine = ""
        form.language = ""
        form.externalSendPolicy = ""
        form.selfName = ""
        form.vocabHintsText = ""
        form.transcriptionModel.mode = .local
        form.minutesModel.mode = .legacy
        let update = try form.makeUpdate()
        let effective = update.applying(to: original)
        XCTAssertEqual(effective.transcriptionEngine, "mlx-whisper")
        XCTAssertEqual(effective.language, "en")
        XCTAssertEqual(effective.externalSendPolicy, "api_ok")
        XCTAssertNil(effective.selfName)
        XCTAssertEqual(effective.vocabHints, [])
        XCTAssertNil(effective.transcriptionModel)
        XCTAssertNil(effective.minutesModel)
        let object = try XCTUnwrap(JSONSerialization.jsonObject(with: JSONEncoder().encode(update)) as? [String: Any])
        XCTAssertTrue(object["transcription_model"] is NSNull)
        XCTAssertTrue(object["minutes_model"] is NSNull)
        XCTAssertNil(object["language"])
    }

    func testNewProfileUsesServerDefaultsForCASAndEffectiveSaveComparison() throws {
        var form = ProfileConfigurationForm()
        form.name = "audio-fixture"
        form.processing.transcriptionModel = TranscriptionModelForm(selection: selection())
        form.processing.externalSendPolicy = "api_ok"
        let original = form.expectedConfig
        let effective = try form.processing.makeUpdate().applying(to: original)
        XCTAssertEqual(original, MeetingConfig.serverDefaults)
        XCTAssertNil(original.transcriptionModel)
        XCTAssertEqual(effective.transcriptionModel, selection())
        XCTAssertEqual(effective.externalSendPolicy, "api_ok")
        XCTAssertEqual(effective.transcriptionEngine, original.transcriptionEngine)
        XCTAssertEqual(effective.language, original.language)
        XCTAssertEqual(form.processing.effectiveLanguage, "ja")
        XCTAssertNotEqual(effective, original)
    }

    func testLegacyUpdateDoesNotSendOrClearUnsupportedASRField() throws {
        var original = MeetingConfig.serverDefaults
        original.externalSendPolicy = "api_ok"
        original.transcriptionModel = selection()
        var form = ProcessingConfigurationForm(config: original)
        XCTAssertThrowsError(try form.makeUpdate(supportsTranscriptionModel: false))
        form.transcriptionModel.mode = .local
        let update = try form.makeUpdate(supportsTranscriptionModel: false)
        let object = try XCTUnwrap(JSONSerialization.jsonObject(with: JSONEncoder().encode(update)) as? [String: Any])
        XCTAssertNil(object["transcription_model"])
        XCTAssertEqual(update.applying(to: original).transcriptionModel, original.transcriptionModel)
    }

    private func selection() -> TranscriptionModelSelection {
        TranscriptionModelSelection(
            connectionID: "conn-111122223333", connectionRevision: 2, modelID: "whisper-1", cacheEpoch: 3)
    }
}
