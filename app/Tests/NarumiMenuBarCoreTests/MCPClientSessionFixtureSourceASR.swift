enum MCPClientSessionFixtureSourceASR {
    static let text = #"""

    private extension Fixture {
        static func checkTranscriptionSelection(
            modelID: String, client: NarumiClient, wire: FakeHTTP
        ) async throws -> [String: Bool] {
            let meetingID = "20260829T000000Z-a1b2c3d4"
            let scope = "fixture-scope"
            let confirmed = try transcriptionConfig(modelID: modelID, epoch: 3)
            let confirmedNode = JSONNode.object(try NarumiClient.arguments(confirmed))
            let defaults = MeetingConfig.serverDefaults
            let defaultsNode = JSONNode.object(try NarumiClient.arguments(defaults))
            let saveID = UUID().uuidString
            var checks: [String: Bool] = [:]
            let saved = try await client.setMeetingConfig(
                meetingID: meetingID, scope: scope, updates: NarumiClient.arguments(confirmed),
                expectedConfig: defaults, requestID: saveID)
            let reloaded = try await client.meeting(id: meetingID, scope: scope)
            var saveArguments = wire.arguments(ToolCatalog.setMeetingConfig).last!
            checks["meeting_cas_sent"] = saveArguments.removeValue(forKey: "expected_config") == defaultsNode
                && saveArguments.removeValue(forKey: "request_id") == .string(saveID)
                && saveArguments.removeValue(forKey: "meeting_id") == .string(meetingID)
                && saveArguments.removeValue(forKey: "scope") == .string(scope)
            checks["meeting_config_round_trip"] = JSONNode.object(saveArguments) == confirmedNode
                && saved.config == confirmed && reloaded.config == confirmed
                && confirmedNode["transcription_model"]?["parameters"] == .object([:])
                && confirmedNode["transcription_model"]?["model_id"] == .string(modelID)

            var stale = confirmed
            stale.vocabHints = ["concurrent change"]
            var meetingConflict = false
            do {
                _ = try await client.setMeetingConfig(
                    meetingID: meetingID, scope: scope, updates: ["self_name": .string("stale replacement")],
                    expectedConfig: stale)
            } catch let failure as ToolFailure { meetingConflict = failure.code == "configuration_conflict" }
            let afterConflict = try await client.meeting(id: meetingID, scope: scope)
            checks["meeting_cas_conflict_preserves_config"] = meetingConflict && afterConflict.config == confirmed

            let profileUpdates: [String: JSONNode] = [
                "config": confirmedNode, "scope": .string(scope), "engagement": .string("fixture-engagement"),
                "export_destinations": .array([.string("markdown")]), "make_default": .bool(false),
            ]
            let profile = try await client.setProfile(
                name: "fixture-transcription", updates: profileUpdates, expectedConfig: defaults)
            let reloadedProfile = try await client.profile(name: "fixture-transcription")
            let profileArguments = wire.arguments(ToolCatalog.setProfile).last!
            checks["profile_cas_sent"] = profileArguments["expected_config"] == defaultsNode
                && UUID(uuidString: profileArguments["request_id"]?.stringValue ?? "") != nil
            checks["profile_round_trip"] = profile.config == confirmed && reloadedProfile == profile
                && profileArguments["config"] == confirmedNode && profileArguments["scope"] == .string(scope)
            var profileConflict = false
            do {
                _ = try await client.setProfile(
                    name: "fixture-transcription", updates: ["config": .object(["language": .string("en")])],
                    expectedConfig: stale)
            } catch let failure as ToolFailure { profileConflict = failure.code == "configuration_conflict" }
            let afterProfileConflict = try await client.profile(name: "fixture-transcription")
            checks["profile_cas_conflict_preserves_config"] = profileConflict && afterProfileConflict == profile

            _ = try await client.listProviderModels(.init(connectionID: "conn-0123456789ab", role: .llm, refresh: false))
            _ = try await client.listProviderModels(.init(connectionID: "conn-0123456789ab", role: .transcription, refresh: false))
            let lists = wire.arguments(ToolCatalog.listProviderModels)
            checks["model_listing_roles_separate"] = lists.count == 2 && lists[0]["role"] == .string("llm")
                && lists[1]["role"] == .string("transcription")
                && lists.allSatisfy { $0["connection_id"] == .string("conn-0123456789ab") && $0["refresh"] == .bool(false) }
            let configurationTools = [ToolCatalog.getServerInfo, ToolCatalog.setMeetingConfig, ToolCatalog.getMeeting,
                ToolCatalog.setProfile, ToolCatalog.getProfile, ToolCatalog.listProviderModels]
            checks["save_and_list_do_not_generate"] = wire.calls.allSatisfy { configurationTools.contains($0) }

            _ = try await client.regenerate(
                meetingID: meetingID, scope: scope, force: false, reason: "fixture audio", expectedConfig: confirmed)
            let generation = wire.arguments(ToolCatalog.regenerate).last!
            checks["expected_config_sent"] = generation["expected_config"] == confirmedNode
                && generation["transcription_retry"] == nil && generation["force"] == nil
                && generation["scope"] == .string(scope) && generation["reason"] == .string("fixture audio")
            var forceRejected = false
            do {
                _ = try await client.regenerate(meetingID: meetingID, scope: scope, force: true, reason: nil, expectedConfig: confirmed)
            } catch let failure as ToolFailure { forceRejected = failure.code == "invalid_argument" }
            checks["force_rejected"] = forceRejected && wire.count(ToolCatalog.regenerate) == 1
            _ = try await client.registerContext(
                meetingID: meetingID, scope: scope, sourceType: "text", payload: .content("fixture text"),
                label: nil, autoRegenerate: true, expectedConfig: confirmed)
            let context = wire.arguments(ToolCatalog.registerContext).last!
            checks["context_expected_config_sent"] = context["expected_config"] == confirmedNode
                && context["auto_regenerate"] == .bool(true) && context["transcription_retry"] == nil
            _ = try await client.registerContext(
                meetingID: meetingID, scope: scope, sourceType: "text", payload: .content("fixture text"),
                label: nil, autoRegenerate: false, expectedConfig: confirmed)
            let notGenerating = wire.arguments(ToolCatalog.registerContext).last!
            checks["non_generating_context_omits_config"] = notGenerating["expected_config"] == nil
                && notGenerating["auto_regenerate"] == nil && notGenerating["transcription_retry"] == nil

            let advanced = try transcriptionConfig(modelID: modelID, epoch: 4)
            let advancedNode = JSONNode.object(try NarumiClient.arguments(advanced))
            let retrySaveID = UUID().uuidString
            let retrySaved = try await client.setMeetingConfig(
                meetingID: meetingID, scope: scope, updates: ["transcription_model": advancedNode["transcription_model"]!],
                expectedConfig: confirmed, requestID: retrySaveID)
            let retrySave = wire.arguments(ToolCatalog.setMeetingConfig).last!
            checks["retry_epoch_save_cas_sent"] = retrySave["expected_config"] == confirmedNode
                && retrySave["request_id"] == .string(retrySaveID) && retrySaved.config == advanced
                && retrySave["transcription_retry"] == nil
            _ = try await client.regenerate(
                meetingID: meetingID, scope: scope, force: false, reason: nil, expectedConfig: retrySaved.config)
            let ordinary = wire.arguments(ToolCatalog.regenerate).last!
            checks["ordinary_epoch_change_has_no_retry"] = ordinary["expected_config"] == advancedNode
                && ordinary["transcription_retry"] == nil && ordinary["force"] == nil

            let retryNode = JSONNode.object([
                "input_fingerprint": .string(String(repeating: "a", count: 64)),
                "chunk_fingerprint": .string(String(repeating: "b", count: 64)), "blocked_epoch": .number(3),
            ])
            let retry = try JSONDecoder().decode(TranscriptionRetry.self, from: retryNode.serialized())
            let retryID = UUID().uuidString
            _ = try await client.regenerate(
                meetingID: meetingID, scope: scope, force: false, reason: nil,
                expectedConfig: retrySaved.config, transcriptionRetry: retry, requestID: retryID)
            let retryArguments = wire.arguments(ToolCatalog.regenerate).last!
            checks["explicit_retry_payload_sent"] = retryArguments["expected_config"] == advancedNode
                && retryArguments["transcription_retry"] == retryNode && retryArguments["force"] == nil
                && retryArguments["request_id"] == .string(retryID)
                && retryArguments["scope"] == .string(scope)
            let invalidRetries: [(String, MeetingConfig?, Bool)] = [
                ("retry_without_config_rejected", nil, false),
                ("retry_without_transcription_rejected", defaults, false),
                ("retry_without_increased_epoch_rejected", confirmed, false),
                ("retry_with_force_rejected", advanced, true),
            ]
            for (name, expected, force) in invalidRetries {
                let before = wire.count(ToolCatalog.regenerate)
                var rejected = false
                do {
                    _ = try await client.regenerate(
                        meetingID: meetingID, scope: scope, force: force, reason: nil,
                        expectedConfig: expected, transcriptionRetry: retry)
                } catch let failure as ToolFailure { rejected = failure.code == "invalid_argument" }
                checks[name] = rejected && wire.count(ToolCatalog.regenerate) == before
            }

            let cleared = try await client.setMeetingConfig(
                meetingID: meetingID, scope: scope, updates: ["transcription_model": .null], expectedConfig: advanced)
            var local = advanced
            local.transcriptionModel = nil
            let reloadedLocal = try await client.meeting(id: meetingID, scope: scope)
            checks["meeting_clear_preserves_local_engine"] = cleared.config == local && reloadedLocal.config == local
                && local.transcriptionEngine == "mlx-whisper"
                && wire.arguments(ToolCatalog.setMeetingConfig).last?["transcription_model"] == .null
            let clearedProfile = try await client.setProfile(
                name: "fixture-transcription", updates: ["config": .object(["transcription_model": .null])], expectedConfig: confirmed)
            var localProfile = confirmed
            localProfile.transcriptionModel = nil
            checks["profile_clear_preserves_local_engine"] = clearedProfile.config == localProfile
                && clearedProfile.config.transcriptionEngine == "mlx-whisper"
                && wire.arguments(ToolCatalog.setProfile).last?["config"]?["transcription_model"] == .null
            return checks
        }

        static func checkTranscriptionLegacy(client: NarumiClient, wire: FakeHTTP) async throws -> [String: Bool] {
            let config = MeetingConfig.serverDefaults
            let form = ProcessingConfigurationForm(config: config)
            let update = try form.makeUpdate(supportsTranscriptionModel: false, supportedTranscriptionProviders: [])
            let arguments = try NarumiClient.arguments(update)
            let saved = try await client.setMeetingConfig(
                meetingID: "20260829T000000Z-a1b2c3d4", scope: nil, updates: arguments)
            let profile = try await client.setProfile(name: "fixture-v4-local", updates: ["config": .object(arguments)])
            let selectedForm = ProcessingConfigurationForm(config: try transcriptionConfig(modelID: "whisper-1", epoch: 0))
            var rejected = false
            do {
                _ = try selectedForm.makeUpdate(supportsTranscriptionModel: false, supportedTranscriptionProviders: [])
            } catch is ConfigurationFormFailure { rejected = true }
            return [
                "v4_meeting_omits_transcription": wire.arguments(ToolCatalog.setMeetingConfig).last?["transcription_model"] == nil
                    && wire.arguments(ToolCatalog.setMeetingConfig).last?["expected_config"] == nil
                    && saved.config.transcriptionModel == nil && saved.config.transcriptionEngine == config.transcriptionEngine,
                "v4_profile_omits_transcription": wire.arguments(ToolCatalog.setProfile).last?["config"]?["transcription_model"] == nil
                    && wire.arguments(ToolCatalog.setProfile).last?["expected_config"] == nil && profile.config.transcriptionModel == nil,
                "v4_transcription_selection_rejected": rejected && wire.count(ToolCatalog.setMeetingConfig) == 1
                    && wire.count(ToolCatalog.setProfile) == 1,
                "v4_save_does_not_generate": wire.count(ToolCatalog.regenerate) == 0 && wire.count(ToolCatalog.registerContext) == 0,
            ]
        }

        static func checkTranscriptionUnknown(client: NarumiClient, valid: Bool) async throws -> [String: Bool] {
            let job = try await client.jobStatus(jobID: "job-0123456789ab")
            let outcome = job.error?.transcriptionOutcome
            let safeJob = job.error.map { !$0.message.contains(fixtureSecret) && !$0.message.contains("議事録生成") } ?? false
            let expectedEvidence = outcome?.inputFingerprint == String(repeating: "a", count: 64)
                && outcome?.chunkFingerprint == String(repeating: "b", count: 64) && outcome?.blockedEpoch == 3
            var safeFailure = false
            do {
                _ = try await client.regenerate(
                    meetingID: "20260829T000000Z-a1b2c3d4", scope: nil, force: false, reason: nil,
                    expectedConfig: transcriptionConfig(modelID: "whisper-1", epoch: 3))
            } catch let failure as ToolFailure {
                safeFailure = failure.code == "engine_unavailable" && !failure.message.contains(fixtureSecret)
                    && !failure.message.contains("議事録生成")
                    && (valid ? failure.transcriptionOutcome == outcome && outcome != nil : failure.transcriptionOutcome == nil)
            }
            if valid {
                return ["transcription_unknown_job_typed": safeJob && expectedEvidence,
                    "transcription_unknown_failure_typed": safeFailure]
            }
            return ["transcription_unknown_malformed_job_blocked": safeJob && outcome == nil,
                "transcription_unknown_malformed_failure_blocked": safeFailure]
        }

        static func checkProfileSaveUnknown(
            client: NarumiClient, wire: FakeHTTP, rawClient: MCPClient
        ) async throws -> [String: Bool] {
            let config = MeetingConfig.serverDefaults
            let updates: [String: JSONNode] = ["config": .object(try NarumiClient.arguments(config))]
            var localized = false
            do {
                _ = try await client.setProfile(name: "fixture-unknown-profile", updates: updates, expectedConfig: config)
            } catch let failure as ToolFailure {
                localized = failure.code == "internal" && failure.transcriptionOutcome == nil
                    && failure.message.contains("保存された可能性") && failure.message.contains("読み込")
                    && !failure.message.contains("議事録生成") && !failure.message.contains(fixtureSecret)
            }
            for _ in 0..<20 { await client.mcp.recoverPendingJobCalls() }
            let reloaded = try await client.profile(name: "fixture-unknown-profile")
            var rawArguments = updates
            rawArguments["name"] = .string("fixture-raw-profile")
            rawArguments["request_id"] = .string(UUID().uuidString)
            var redacted = false
            do {
                _ = try await rawClient.callTool(ToolCatalog.setProfile, arguments: rawArguments)
            } catch let error as MCPClientError {
                if case .tool(let message, let payload) = error {
                    redacted = payload?["error"]?["details"] == nil
                        && payload?["error"]?["code"] == .string("internal")
                        && message.contains("保存された可能性") && !message.contains(fixtureSecret)
                }
            }
            return [
                "profile_save_unknown_localized_no_replay": localized && reloaded.config == config
                    && wire.count(ToolCatalog.setProfile) == 1 && wire.count(ToolCatalog.getProfile) == 1,
                "profile_save_unknown_details_redacted": redacted,
            ]
        }

        static func transcriptionConfig(modelID: String, epoch: Int) throws -> MeetingConfig {
            let node = JSONNode.object([
                "provider": .string("openai-api"), "connection_id": .string("conn-0123456789ab"),
                "connection_revision": .number(4), "model_id": .string(modelID),
                "parameters": .object([:]), "cache_epoch": .number(Double(epoch)),
            ])
            var config = MeetingConfig.serverDefaults
            config.transcriptionEngine = "mlx-whisper"
            config.externalSendPolicy = "api_ok"
            config.language = modelID == "whisper-1" ? "ja" : "auto"
            config.selfName = "Fixture Owner"
            config.vocabHints = ["fixture vocabulary"]
            config.transcriptionModel = try JSONDecoder().decode(TranscriptionModelSelection.self, from: node.serialized())
            return config
        }
    }

    """#
}
