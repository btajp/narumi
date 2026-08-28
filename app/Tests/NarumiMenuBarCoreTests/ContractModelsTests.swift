import XCTest

@testable import NarumiMenuBarCore

/// Decodes the `examples.output` entries of every `contracts/tools/*.json` (verbatim, straight
/// from the repository checkout) with the Swift model the window uses. A renamed key or a
/// wrongly-optional field fails here before it fails silently in the UI.
final class ContractModelsTests: XCTestCase {
    private struct ContractsNotFound: Error {}

    private func toolsDirectory() throws -> URL {
        var directory = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
        while true {
            let candidate = directory.appendingPathComponent("contracts/tools")
            if FileManager.default.fileExists(atPath: candidate.path) {
                return candidate
            }
            let parent = directory.deletingLastPathComponent()
            guard parent.path != directory.path else {
                XCTFail("contracts/tools not found above \(#filePath)")
                throw ContractsNotFound()
            }
            directory = parent
        }
    }

    /// The verbatim `examples.output` array of one tool contract, re-serialized per element.
    private func exampleOutputs(_ tool: String) throws -> [Data] {
        let url = try toolsDirectory().appendingPathComponent("\(tool).json")
        let root = try JSONSerialization.jsonObject(with: Data(contentsOf: url)) as? [String: Any]
        guard let examples = root?["examples"] as? [String: Any],
            let outputs = examples["output"] as? [Any]
        else {
            XCTFail("\(tool).json has no examples.output")
            return []
        }
        return try outputs.map { try JSONSerialization.data(withJSONObject: $0) }
    }

    @discardableResult
    private func decodeAll<T: Decodable>(_ type: T.Type, tool: String) throws -> [T] {
        let outputs = try exampleOutputs(tool)
        XCTAssertFalse(outputs.isEmpty, "\(tool) should carry at least one example output")
        return try outputs.enumerated().map { index, data in
            do {
                return try JSONDecoder().decode(T.self, from: data)
            } catch {
                XCTFail("\(tool) example output #\(index) does not decode as \(T.self): \(error)")
                throw error
            }
        }
    }

    // MARK: Gaia connection

    func testGetGaiaConnection() throws {
        let responses = try decodeAll(GaiaConnectionResponse.self, tool: "get_gaia_connection")
        XCTAssertNil(responses[0].connection.url)
        XCTAssertFalse(responses[0].connection.hasAPIKey)
        XCTAssertEqual(responses[0].connection.source, .unconfigured)
        XCTAssertEqual(responses[1].connection.url, "http://127.0.0.1:4111/mcp")
        XCTAssertTrue(responses[1].connection.hasAPIKey)
        XCTAssertEqual(responses[1].connection.source, .saved)
    }

    func testSetGaiaConnection() throws {
        let responses = try decodeAll(GaiaConnectionResponse.self, tool: "set_gaia_connection")
        XCTAssertTrue(responses[0].connection.hasAPIKey)
        XCTAssertNil(responses[1].connection.url)
        XCTAssertFalse(responses[1].connection.hasAPIKey)
        XCTAssertEqual(responses[1].connection.source, .saved)
    }

    func testTestGaiaConnection() throws {
        let responses = try decodeAll(GaiaConnectionTestResult.self, tool: "test_gaia_connection")
        let result = try XCTUnwrap(responses.first)
        XCTAssertTrue(result.connected)
        XCTAssertEqual(result.name, "gaia_library")
        XCTAssertEqual(result.version, "0.1.0")
        XCTAssertEqual(result.contractVersion, "1.0.0")
        XCTAssertEqual(result.client.name, "narumi")
        XCTAssertEqual(result.client.role, .agent)
        XCTAssertEqual(result.client.defaultScope, "cloudnative")
    }

    // MARK: Meetings / search

    func testListMeetings() throws {
        let responses = try decodeAll(ListMeetingsResponse.self, tool: "list_meetings")
        let meetings = try XCTUnwrap(responses.first).meetings
        XCTAssertEqual(meetings.count, 3)
        XCTAssertEqual(meetings[0].meetingName, "週次定例")
        XCTAssertEqual(meetings[0].status, "ready")
        XCTAssertNil(meetings[0].activeJob)  // explicit JSON null
        XCTAssertEqual(meetings[0].latestMinutesVersion, 2)
        XCTAssertNil(meetings[1].activeJob)  // key absent
        let job = try XCTUnwrap(meetings[2].activeJob)
        XCTAssertEqual(job.kind, "process")
        XCTAssertEqual(job.status, "running")
        XCTAssertEqual(job.progress?.stage, "transcribe")
        XCTAssertEqual(job.progress?.fraction, 0.4)
    }

    func testSearchTranscripts() throws {
        let responses = try decodeAll(SearchTranscriptsResponse.self, tool: "search_transcripts")
        let hits = try XCTUnwrap(responses.first).hits
        XCTAssertEqual(hits.count, 2)
        XCTAssertNil(hits[0].speaker)
        XCTAssertEqual(hits[1].speaker, "岡村")
        XCTAssertEqual(hits[1].start, 130.2)
        XCTAssertTrue(responses.last?.hits.isEmpty ?? false)
    }

    // MARK: Recording

    func testConfigureRecordingPermission() throws {
        let responses = try decodeAll(
            ConfigureRecordingPermissionResponse.self, tool: ToolCatalog.configureRecordingPermission)
        XCTAssertEqual(responses.count, 3)
        XCTAssertEqual(responses[0].permission, .microphone)
        XCTAssertEqual(responses[0].action, .request)
        XCTAssertEqual(responses[0].permissions.microphone, "granted")
        XCTAssertFalse(responses[0].settingsOpened)
        XCTAssertEqual(responses[1].permission, .screenRecording)
        XCTAssertEqual(responses[1].action, .openSettings)
        XCTAssertTrue(responses[1].settingsOpened)
        XCTAssertEqual(responses[2].permissions.microphone, "denied")
    }

    func testRecordingStatus() throws {
        let statuses = try decodeAll(RecordingStatus.self, tool: "get_recording_status")
        XCTAssertEqual(statuses.count, 2)
        XCTAssertFalse(statuses[0].active)
        XCTAssertNil(statuses[0].meetingID)
        XCTAssertTrue(statuses[1].active)
        XCTAssertEqual(statuses[1].meetingName, "週次定例")
        XCTAssertEqual(statuses[1].elapsedSec, 754.3)
        XCTAssertEqual(statuses[1].tracks?["mic"], "tracks/mic.m4a")
    }

    // MARK: Meeting detail / minutes / transcript

    func testGetMeeting() throws {
        let details = try decodeAll(MeetingDetail.self, tool: "get_meeting")
        let detail = try XCTUnwrap(details.first)
        XCTAssertEqual(detail.meeting.meetingID, "20260827T030500Z-a1b2c3d4")
        XCTAssertEqual(detail.config.selfName, "岡村")
        XCTAssertEqual(detail.config.vocabHints, ["gaia-library"])
        XCTAssertEqual(detail.recording.tracks.count, 3)
        XCTAssertTrue(try XCTUnwrap(detail.recording.tracks["screen"]).discarded)
        XCTAssertNil(detail.recording.tracks["screen"]?.sha256)  // JSON null survives as nil
        XCTAssertEqual(detail.contexts.first?.sourceType, "notion_ai_minutes")
        XCTAssertEqual(detail.minutesVersions.map(\.version), [1, 2])
        XCTAssertEqual(detail.latestMinutes?.version, 2)
        XCTAssertEqual(detail.exports.first?.minutesVersion, 2)
        XCTAssertFalse(detail.artifacts.isEmpty)
    }

    func testGetMinutes() throws {
        let all = try decodeAll(Minutes.self, tool: "get_minutes")
        let minutes = try XCTUnwrap(all.first)
        XCTAssertEqual(minutes.version, 2)
        XCTAssertEqual(minutes.unresolvedSpeakers, ["SPEAKER_01"])
        XCTAssertEqual(minutes.availableVersions, [1, 2])
        XCTAssertTrue(minutes.markdown.hasPrefix("# 週次定例"))
    }

    func testGetTranscript() throws {
        let transcripts = try decodeAll(Transcript.self, tool: "get_transcript")
        let transcript = try XCTUnwrap(transcripts.first)
        XCTAssertEqual(transcript.source, "merged")
        XCTAssertEqual(transcript.segments.count, 2)
        XCTAssertEqual(transcript.segments[0].speakerName, "岡村")
        XCTAssertNil(transcript.segments[1].speakerName)  // JSON null
        XCTAssertEqual(transcript.speakerMap["me"]?.name, "岡村")
        XCTAssertNil(transcript.speakerMap["other"]?.name)
        XCTAssertEqual(transcript.availableSources, ["merged", "own-mic", "own-system"])
    }

    // MARK: Jobs

    func testGetJobStatus() throws {
        let responses = try decodeAll(JobStatusResponse.self, tool: "get_job_status")
        XCTAssertEqual(responses[0].job.status, "running")
        XCTAssertTrue(responses[0].job.isActive)
        XCTAssertEqual(responses[1].job.status, "failed")
        XCTAssertFalse(responses[1].job.isActive)
        XCTAssertEqual(responses[1].job.error?.code, "policy_violation")
    }

    func testCancelJob() throws {
        let responses = try decodeAll(JobStatusResponse.self, tool: "cancel_job")
        XCTAssertEqual(responses[0].job.status, "cancelled")
        XCTAssertEqual(responses[0].job.error?.code, "cancelled")
    }

    func testRegenerate() throws {
        let responses = try decodeAll(RegenerateResponse.self, tool: "regenerate")
        XCTAssertEqual(responses.first?.jobID, "job-0123456789ab")
    }

    // MARK: Context / config / export

    func testRegisterContext() throws {
        let responses = try decodeAll(RegisterContextResponse.self, tool: "register_context")
        XCTAssertEqual(responses[0].status, "parsed")
        XCTAssertNil(responses[0].jobID)
        XCTAssertEqual(responses[1].jobID, "job-abcdef012345")
    }

    func testSetMeetingConfig() throws {
        let responses = try decodeAll(SetMeetingConfigResponse.self, tool: "set_meeting_config")
        XCTAssertEqual(responses.first?.config.llmProvider, "claude-agent-sdk")
        XCTAssertEqual(responses.first?.scope, "cloudnative")
    }

    func testExportMinutes() throws {
        let responses = try decodeAll(ExportMinutesResponse.self, tool: "export_minutes")
        XCTAssertEqual(responses[0].result?.destination, "markdown")
        XCTAssertNil(responses[0].jobID)
        XCTAssertNil(responses[1].result)
        XCTAssertEqual(responses[1].jobID, "job-fedcba987654")
    }

    func testListExportDestinations() throws {
        let responses = try decodeAll(
            ListExportDestinationsResponse.self, tool: "list_export_destinations")
        let destinations = try XCTUnwrap(responses.first).destinations
        XCTAssertEqual(destinations.map(\.name), ["markdown", "html"])
    }

    // MARK: Destructive / import

    func testDiscardTracks() throws {
        let responses = try decodeAll(DiscardTracksResponse.self, tool: "discard_tracks")
        let tracks = try XCTUnwrap(responses.first).tracks
        XCTAssertTrue(try XCTUnwrap(tracks["screen"]).discarded)
        XCTAssertNil(tracks["screen"]?.bytes)
        XCTAssertFalse(try XCTUnwrap(tracks["mic"]).discarded)
    }

    func testDeleteMeeting() throws {
        let responses = try decodeAll(DeleteMeetingResponse.self, tool: "delete_meeting")
        XCTAssertTrue(try XCTUnwrap(responses.first).deleted)
        XCTAssertTrue(try XCTUnwrap(responses.first).movedTo.contains("/trash/"))
    }

    func testImportRecording() throws {
        let responses = try decodeAll(ImportRecordingResponse.self, tool: "import_recording")
        let response = try XCTUnwrap(responses.first)
        XCTAssertEqual(response.meetingID, "20260825T090000Z-00c0ffee")
        XCTAssertEqual(response.jobID, "job-abcdef012345")
        XCTAssertEqual(response.tracks["system"]?.durationSec, 3595.0)
    }

    // MARK: Profiles

    func testListProfiles() throws {
        let responses = try decodeAll(ListProfilesResponse.self, tool: "list_profiles")
        let response = try XCTUnwrap(responses.first)
        XCTAssertEqual(response.defaultName, "default")
        XCTAssertEqual(response.profiles.count, 2)
        XCTAssertTrue(try XCTUnwrap(response.profiles.first).isDefault)
        XCTAssertEqual(response.profiles[1].exportDestinations, ["markdown"])
        XCTAssertNil(response.profiles[1].engagement)
    }

    func testGetProfile() throws {
        let responses = try decodeAll(ProfileResponse.self, tool: "get_profile")
        XCTAssertEqual(responses.first?.profile.name, "customer-meetings")
        XCTAssertEqual(responses.first?.profile.config.externalSendPolicy, "api_ok")
    }

    func testSetProfile() throws {
        let responses = try decodeAll(ProfileResponse.self, tool: "set_profile")
        XCTAssertEqual(responses.first?.profile.scope, "cloudnative")
    }

    func testDeleteProfile() throws {
        let responses = try decodeAll(DeleteProfileResponse.self, tool: "delete_profile")
        XCTAssertTrue(try XCTUnwrap(responses.first).deleted)
    }

    // MARK: Server info / catalog

    func testGetServerInfo() throws {
        let infos = try decodeAll(ServerInfo.self, tool: "get_server_info")
        XCTAssertEqual(infos.count, 2)
        let http = infos[0]
        XCTAssertEqual(http.serverInstanceID, "00000000-0000-4000-8000-000000000001")
        XCTAssertTrue(http.capabilities.recording)
        XCTAssertFalse(http.capabilities.permissionSetupInProgress)
        XCTAssertEqual(http.capabilities.workflow?.providerConnections, true)
        XCTAssertEqual(http.capabilities.workflow?.providerModels, true)
        XCTAssertEqual(http.capabilities.workflow?.stageModelSelection, true)
        XCTAssertEqual(http.capabilities.workflow?.ensembleGeneration, false)
        XCTAssertEqual(http.secureTransport?.mode, "pinned_tls")
        XCTAssertEqual(http.secureTransport?.tlsRequired, true)
        XCTAssertEqual(http.secureTransport?.clientAuthRequired, true)
        XCTAssertEqual(http.capabilities.permissions?.screenRecording, "granted")
        XCTAssertEqual(http.diagnostics.ffmpeg?.version, "7.1.1")
        XCTAssertEqual(http.diagnostics.recorderPath, "/Applications/narumi.app/Contents/MacOS/narumi-recorder")
        let stdio = infos[1]
        XCTAssertEqual(stdio.serverInstanceID, "00000000-0000-4000-8000-000000000002")
        XCTAssertFalse(stdio.capabilities.recording)
        XCTAssertFalse(stdio.capabilities.permissionSetupInProgress)
        XCTAssertNil(stdio.capabilities.permissions)
        XCTAssertNil(stdio.diagnostics.ffmpeg)  // JSON null
        XCTAssertNil(stdio.diagnostics.recorderPath)
    }

    func testServerInfoKeepsMissingInstanceIDCompatibleWithOlderServersAndInitializers() throws {
        let example = try XCTUnwrap(try exampleOutputs("get_server_info").first)
        var object = try XCTUnwrap(try JSONSerialization.jsonObject(with: example) as? [String: Any])
        object.removeValue(forKey: "server_instance_id")
        let legacy = try JSONDecoder().decode(ServerInfo.self, from: JSONSerialization.data(withJSONObject: object))
        XCTAssertNil(legacy.serverInstanceID)
        let initialized = ServerInfo(
            name: legacy.name, serverVersion: legacy.serverVersion, contractVersion: legacy.contractVersion,
            capabilities: legacy.capabilities, diagnostics: legacy.diagnostics,
            secureTransport: legacy.secureTransport)
        XCTAssertEqual(initialized, legacy)
        let roundTrip = try JSONDecoder().decode(ServerInfo.self, from: JSONEncoder().encode(initialized))
        XCTAssertEqual(roundTrip, legacy)
    }

    func testServerInfoRejectsPresentNullWrongTypeOrInvalidInstanceIDs() throws {
        let example = try XCTUnwrap(try exampleOutputs("get_server_info").first)
        let base = try XCTUnwrap(try JSONSerialization.jsonObject(with: example) as? [String: Any])
        let invalidIDs: [Any] = [
            NSNull(), 1, true, [String](), [String: String](), "", "invalid",
            "12345678-9ABC-4DEF-8ABC-123456789ABC", "12345678-9abc-1def-8abc-123456789abc",
            "12345678-9abc-4def-cabc-123456789abc", "12345678-9abc-4def-8abc-123456789abc\n",
        ]
        for value in invalidIDs {
            var object = base
            object["server_instance_id"] = value
            let data = try JSONSerialization.data(withJSONObject: object)
            XCTAssertThrowsError(try JSONDecoder().decode(ServerInfo.self, from: data))
        }
    }

    func testRebuildCatalog() throws {
        let responses = try decodeAll(RebuildCatalogResponse.self, tool: "rebuild_catalog")
        XCTAssertEqual(responses[0].meetings, 42)
        XCTAssertTrue(responses[0].errors.isEmpty)
        XCTAssertEqual(responses[1].errors.count, 1)
    }
}
