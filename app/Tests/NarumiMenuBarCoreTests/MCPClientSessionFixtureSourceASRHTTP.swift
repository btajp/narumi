enum MCPClientSessionFixtureSourceASRHTTP {
    static let text = #"""

    private extension FakeHTTP {
        func transcriptionResponse(_ request: URLRequest, id: JSONNode, name: String) throws -> (Data, URLResponse)? {
            guard scenario.hasPrefix("transcription_") || scenario == "minutes_retry_response_lost" else { return nil }
            let input = arguments(name).last ?? [:]
            let defaults = try NarumiClient.arguments(MeetingConfig.serverDefaults)
            if name == ToolCatalog.setMeetingConfig {
                let current = lock.withLock { savedConfig.isEmpty ? defaults : savedConfig }
                guard try configurationMatches(input["expected_config"], current: .object(current)) else {
                    return try transcriptionError(request, id: id, code: "configuration_conflict")
                }
                let keys: Set<String> = [
                    "transcription_engine", "transcription_model", "diarization_engine", "llm_provider",
                    "minutes_model", "external_send_policy", "language", "self_name", "vocab_hints",
                ]
                let updates = input.filter { keys.contains($0.key) }
                let stored = lock.withLock {
                    savedConfig = current.merging(updates) { _, replacement in replacement }
                    return savedConfig
                }
                return try structured(request, id: id, value: .object([
                    "meeting_id": .string("20260829T000000Z-a1b2c3d4"), "config": .object(stored),
                    "scope": input["scope"] ?? .null,
                ]))
            }
            if name == ToolCatalog.getMeeting {
                let current = lock.withLock { savedConfig.isEmpty ? defaults : savedConfig }
                return try structured(request, id: id, value: .object([
                    "meeting": .object([
                        "meeting_id": .string("20260829T000000Z-a1b2c3d4"), "meeting_name": .string("Fixture meeting"),
                        "scope": input["scope"] ?? .null, "status": .string("recorded"),
                        "started_at": .string("2026-08-29T00:00:00Z"),
                    ]),
                    "bundle_path": .string("/fixture/meeting"), "config": .object(current),
                    "recording": .object(["tracks": .object([:])]), "contexts": .array([]),
                    "minutes_versions": .array([]), "latest_minutes": .null,
                    "exports": .array([]), "artifacts": .array([]),
                ]))
            }
            if name == ToolCatalog.setProfile {
                let current = lock.withLock { savedProfile["config"] ?? .object(defaults) }
                guard try configurationMatches(input["expected_config"], current: current) else {
                    return try transcriptionError(request, id: id, code: "configuration_conflict")
                }
                guard case .object(let config) = current, let update = input["config"], case .object(let updates) = update else {
                    return try transcriptionError(request, id: id, code: "invalid_argument")
                }
                let profile = JSONNode.object([
                    "name": input["name"]!, "config": .object(config.merging(updates) { _, replacement in replacement }),
                    "scope": input["scope"] ?? .null, "engagement": input["engagement"] ?? .null,
                    "export_destinations": input["export_destinations"] ?? .array([]),
                    "is_default": input["make_default"] ?? .bool(false),
                ])
                lock.withLock { savedProfile = profile }
                if scenario == "transcription_profile_save_unknown" {
                    return try transcriptionError(request, id: id, code: "internal", details: .object([
                        "reason": .string("profile_save_outcome_unknown"), "outcome_unknown": .bool(true),
                        "raw_response": .string(fixtureSecret),
                    ]))
                }
                return try structured(request, id: id, value: .object(["profile": profile]))
            }
            if name == ToolCatalog.getProfile {
                return try structured(request, id: id, value: .object(["profile": lock.withLock { savedProfile }]))
            }
            if name == ToolCatalog.listProviderModels {
                return try structured(request, id: id, value: .object([
                    "connection_id": input["connection_id"]!, "connection_revision": .number(4),
                    "models": .array([]), "next_cursor": .null, "catalog_state": .string("ready"),
                    "fetched_at": .string("2026-08-29T00:00:00Z"),
                ]))
            }
            if name == ToolCatalog.getJobStatus {
                return try structured(request, id: id, value: .object([
                    "job": .object([
                        "job_id": .string("job-0123456789ab"), "meeting_id": .string("20260829T000000Z-a1b2c3d4"),
                        "kind": .string("regenerate"), "status": .string("failed"),
                        "progress": .null, "result": .null,
                        "error": .object([
                            "code": .string("engine_unavailable"), "message": .string(fixtureSecret),
                            "details": transcriptionUnknownDetails,
                        ]),
                        "created_at": .string("2026-08-29T00:00:00Z"), "updated_at": .string("2026-08-29T00:00:01Z"),
                    ]),
                ]))
            }
            if name == ToolCatalog.regenerate {
                if scenario.hasPrefix("transcription_manual_") {
                    if count(name) == 1 {
                        lock.withLock { acceptedGenerationRequests += 1 }
                        return response(request, status: 200, body: Data("discarded response".utf8))
                    }
                    let outcome = String(scenario.dropFirst("transcription_manual_".count))
                    if ["configuration_conflict", "authentication_required", "model_unavailable"].contains(outcome) {
                        return try transcriptionError(request, id: id, code: outcome)
                    }
                    return try transcriptionReceipt(request, id: id, outcome: outcome)
                }
                if scenario.hasPrefix("transcription_retry_rejected_") {
                    return try transcriptionError(request, id: id, code: String(scenario.dropFirst("transcription_retry_rejected_".count)))
                }
                if scenario == "transcription_retry_unreached" { throw URLError(.cannotConnectToHost) }
                if ["transcription_retry_wrong_meeting", "transcription_retry_invalid_receipt"].contains(scenario) {
                    lock.withLock { acceptedGenerationRequests += 1 }
                    return try transcriptionReceipt(request, id: id, outcome: String(scenario.dropFirst("transcription_retry_".count)))
                }
                if ["transcription_retry_response_lost", "transcription_retry_cancelled"].contains(scenario) {
                    lock.withLock { acceptedGenerationRequests += 1 }
                    if scenario == "transcription_retry_cancelled" { throw CancellationError() }
                    return response(request, status: 200, body: Data(("discarded response " + fixtureSecret).utf8))
                }
                if ["transcription_ordinary_response_lost", "minutes_retry_response_lost"].contains(scenario) {
                    if count(name) == 1 {
                        lock.withLock { acceptedGenerationRequests += 1 }
                        return response(request, status: 200, body: Data("discarded response".utf8))
                    }
                    return try structured(request, id: id, value: .object([
                        "job_id": .string("job-0123456789ab"), "meeting_id": .string("20260829T000000Z-a1b2c3d4"),
                    ]))
                }
                if scenario != "transcription_requests" {
                    return try transcriptionError(request, id: id, code: "engine_unavailable", details: transcriptionUnknownDetails)
                }
                return try structured(request, id: id, value: .object([
                    "job_id": .string("job-0123456789ab"), "meeting_id": .string("20260829T000000Z-a1b2c3d4"),
                ]))
            }
            if name == ToolCatalog.registerContext {
                return try structured(request, id: id, value: .object([
                    "context_id": .string("context-fixture"), "status": .string("parsed"), "job_id": .null,
                ]))
            }
            return nil
        }

        private func transcriptionReceipt(_ request: URLRequest, id: JSONNode, outcome: String) throws -> (Data, URLResponse) {
            try structured(request, id: id, value: .object([
                "job_id": .string(outcome == "invalid_receipt" ? "not-a-job" : "job-0123456789ab"),
                "meeting_id": .string(outcome == "wrong_meeting" ? "20260829T000000Z-deadbeef" : "20260829T000000Z-a1b2c3d4"),
            ]))
        }

        private var transcriptionUnknownDetails: JSONNode {
            var details: [String: JSONNode] = [
                "stage": .string("transcribe"), "reason": .string("transcription_outcome_unknown"),
                "outcome_unknown": .bool(true), "input_fingerprint": .string(String(repeating: "a", count: 64)),
                "chunk_fingerprint": .string(String(repeating: "b", count: 64)), "blocked_epoch": .number(3),
                "track": .string("system"), "chunk_index": .number(1), "chunk_count": .number(3),
                "completed_chunks": .number(1), "start_sample": .number(0), "end_sample": .number(9_600_000),
                "sample_rate": .number(16_000),
            ]
            if scenario == "transcription_unknown_invalid" { details.removeValue(forKey: "sample_rate") }
            return .object(details)
        }

        private func configurationMatches(_ expected: JSONNode?, current: JSONNode) throws -> Bool {
            guard let expected else { return true }
            let decoder = JSONDecoder()
            let before = try decoder.decode(MeetingConfig.self, from: expected.serialized())
            let stored = try decoder.decode(MeetingConfig.self, from: current.serialized())
            return before == stored
        }

        private func transcriptionError(
            _ request: URLRequest, id: JSONNode, code: String, details: JSONNode? = nil
        ) throws -> (Data, URLResponse) {
            var error: [String: JSONNode] = ["code": .string(code), "message": .string(details == nil ? code : fixtureSecret)]
            if let details { error["details"] = details }
            return try result(request, id: id, value: .object([
                "structuredContent": .object(["error": .object(error)]), "isError": .bool(true), "content": .array([]),
            ]))
        }
    }

    """#
}
