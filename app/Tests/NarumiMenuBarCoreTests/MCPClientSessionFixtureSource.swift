enum MCPClientSessionFixtureSource {
    static let text = #"""
    import Darwin
    import Foundation

    private let instance = "00000000-0000-4000-8000-000000000001"
    private let requestID = "provider-request-original-1234"
    private let fixtureSecret = "fixture-secret-never-production"

    private struct FakeBootstrap: MCPServerBootstrapLoading {
        let connection: MCPServerConnection
        let fail: Bool
        func load(expectedURL: URL) throws -> MCPServerConnection {
            if fail { throw MCPConnectionError.unsafeBootstrap }
            return connection
        }
    }

    private final class FakeHTTP: MCPHTTPTransporting, @unchecked Sendable {
        let scenario: String
        private let lock = NSLock()
        private var tools: [String] = []
        private var methods: [String] = []
        private var argumentsByTool: [String: [[String: JSONNode]]] = [:]
        private var httpMethodsByTool: [String: [String]] = [:]
        private var savedConfig: [String: JSONNode] = [:]
        private var savedProfile: JSONNode = .object([:])
        private var acceptedGenerationRequests = 0
        init(_ scenario: String) { self.scenario = scenario }
        func invalidate() {}
        func count(_ tool: String) -> Int { lock.withLock { tools.filter { $0 == tool }.count } }
        var calls: [String] { lock.withLock { tools } }
        var allMethods: [String] { lock.withLock { methods } }
        var acceptedGenerationCount: Int { lock.withLock { acceptedGenerationRequests } }
        func arguments(_ tool: String) -> [[String: JSONNode]] { lock.withLock { argumentsByTool[tool] ?? [] } }
        func httpMethods(_ tool: String) -> [String] { lock.withLock { httpMethodsByTool[tool] ?? [] } }

        private var contractVersion: String {
            switch scenario {
            case "v1": return "1.1.0"
            case "future_contract": return "7.0.0"
            case "v6_success": return "6.0.0"
            case "codex_minutes_requests": return "3.0.0"
            case "minutes_requests", "minutes_retry_response_lost": return "4.0.0"
            default: return scenario.hasPrefix("transcription_") ? "5.0.0" : "2.0.0"
            }
        }

        func data(for request: URLRequest, protectingSecrets: Bool) async throws -> (Data, URLResponse) {
            let rpc = try JSONNode.parse(request.httpBody!)
            let method = rpc["method"]!.stringValue!
            lock.withLock { methods.append(method) }
            if method == "notifications/initialized" { return response(request, status: 202, body: Data()) }
            let id = rpc["id"]!
            if method == "initialize" { return try result(request, id: id, value: .object([:])) }
            let name = rpc["params"]!["name"]!.stringValue!
            lock.withLock {
                tools.append(name)
                httpMethodsByTool[name, default: []].append(request.httpMethod ?? "")
            }
            if let value = rpc["params"]?["arguments"], case .object(let arguments) = value {
                lock.withLock { argumentsByTool[name, default: []].append(arguments) }
            }
            if name == ToolCatalog.getServerInfo {
                var info: [String: JSONNode] = [
                    "contract_version": .string(contractVersion),
                    "server_instance_id": .string(scenario == "wrong_instance" ? "00000000-0000-4000-8000-000000000002" : instance),
                    "secure_transport": .object([
                        "mode": .string("pinned_tls"), "tls_required": .bool(true),
                        "client_auth_required": .bool(scenario != "false_auth"),
                    ]),
                ]
                if scenario == "missing_tls" { info.removeValue(forKey: "secure_transport") }
                return try structured(request, id: id, value: .object(info))
            }
            if name == ToolCatalog.setProviderConnection, scenario == "secret404" {
                return response(request, status: 404, body: Data(fixtureSecret.utf8))
            }
            if name == ToolCatalog.prepareProviderRuntime, scenario == "setup404" {
                return response(request, status: 404, body: Data())
            }
            if name == ToolCatalog.listProviders, scenario == "readonly404", count(name) == 1 {
                return response(request, status: 404, body: Data())
            }
            if scenario == "rpc_error" {
                return response(request, status: 200, body: try JSONNode.object([
                    "jsonrpc": .string("2.0"), "id": id,
                    "error": .object(["code": .number(-32603), "message": .string(fixtureSecret)]),
                ]).serialized())
            }
            if scenario == "malformed" {
                return response(request, status: 200, body: Data(("invalid-json " + fixtureSecret).utf8))
            }
            if name == ToolCatalog.verifyProviderModel, scenario.hasPrefix("provider_verify_") {
                let reason = scenario == "provider_verify_unknown"
                    ? "provider_generation_outcome_unknown" : "model_generation_verification_failed"
                var details: [String: JSONNode] = [
                    "reason": .string(reason), "untrusted_detail": .string(fixtureSecret),
                ]
                if scenario == "provider_verify_unknown" || scenario == "provider_verify_malformed" {
                    details["outcome_unknown"] = .bool(true)
                }
                return try result(request, id: id, value: .object([
                    "structuredContent": .object(["error": .object([
                        "code": .string("engine_unavailable"), "message": .string(fixtureSecret),
                        "details": .object(details),
                    ])]),
                    "isError": .bool(true),
                    "content": .array([.object(["type": .string("text"), "text": .string(fixtureSecret)])]),
                ]))
            }
            if let transcription = try transcriptionResponse(request, id: id, name: name) {
                return transcription
            }
            if ["minutes_requests", "codex_minutes_requests", "legacy_minutes_requests"].contains(scenario) {
                let input = arguments(name).last ?? [:]
                if name == ToolCatalog.setMeetingConfig {
                    let configurationKeys: Set<String> = [
                        "transcription_engine", "diarization_engine", "llm_provider", "external_send_policy",
                        "language", "self_name", "vocab_hints", "minutes_model",
                    ]
                    let updates = input.filter { configurationKeys.contains($0.key) }
                    let config = lock.withLock {
                        savedConfig.merge(updates) { _, replacement in replacement }
                        return savedConfig
                    }
                    return try structured(request, id: id, value: .object([
                        "meeting_id": .string("20260829T000000Z-a1b2c3d4"), "config": .object(config),
                        "scope": input["scope"] ?? .null,
                    ]))
                }
                if name == ToolCatalog.getMeeting {
                    let config = lock.withLock { savedConfig }
                    return try structured(request, id: id, value: .object([
                        "meeting": .object([
                            "meeting_id": .string("20260829T000000Z-a1b2c3d4"), "meeting_name": .string("Fixture meeting"),
                            "scope": input["scope"] ?? .null, "status": .string("recorded"),
                            "started_at": .string("2026-08-29T00:00:00Z"),
                        ]),
                        "bundle_path": .string("/fixture/meeting"), "config": .object(config),
                        "recording": .object(["tracks": .object([:])]), "contexts": .array([]),
                        "minutes_versions": .array([]), "latest_minutes": .null,
                        "exports": .array([]), "artifacts": .array([]),
                    ]))
                }
                if name == ToolCatalog.setProfile {
                    let profile = JSONNode.object([
                        "name": input["name"]!, "config": input["config"]!,
                        "scope": input["scope"] ?? .null, "engagement": input["engagement"] ?? .null,
                        "export_destinations": input["export_destinations"] ?? .array([]),
                        "is_default": input["make_default"] ?? .bool(false),
                    ])
                    lock.withLock { savedProfile = profile }
                    return try structured(request, id: id, value: .object(["profile": profile]))
                }
                if name == ToolCatalog.getProfile {
                    let profile = lock.withLock { savedProfile }
                    return try structured(request, id: id, value: .object(["profile": profile]))
                }
                if name == ToolCatalog.regenerate {
                    return try structured(request, id: id, value: .object([
                        "job_id": .string("job-0123456789ab"), "meeting_id": .string("20260829T000000Z-a1b2c3d4"),
                    ]))
                }
                if name == ToolCatalog.registerContext {
                    return try structured(request, id: id, value: .object([
                        "context_id": .string("context-fixture"), "status": .string("parsed"), "job_id": .null,
                    ]))
                }
            }
            var providers: [JSONNode] = []
            if scenario == "setup404" {
                providers = [.object([
                    "provider_id": .string("claude-agent-sdk"), "runtime": .object([
                        "active_setup": .object([
                            "start_request_id": .string(requestID), "resource_id": .string("claude-sdk"),
                            "job_id": .string("job-0123456789ab"), "state": .string("running"),
                        ]), "last_setup": .null,
                    ]),
                ])]
            }
            return try structured(request, id: id, value: .object(["providers": .array(providers)]))
        }

        private func structured(_ request: URLRequest, id: JSONNode, value: JSONNode) throws -> (Data, URLResponse) {
            try result(request, id: id, value: .object([
                "structuredContent": value, "isError": .bool(false), "content": .array([]),
            ]))
        }
        private func result(_ request: URLRequest, id: JSONNode, value: JSONNode) throws -> (Data, URLResponse) {
            response(request, status: 200, body: try JSONNode.object([
                "jsonrpc": .string("2.0"), "id": id, "result": value,
            ]).serialized())
        }
        private func response(_ request: URLRequest, status: Int, body: Data) -> (Data, URLResponse) {
            (body, HTTPURLResponse(url: request.url!, statusCode: status, httpVersion: "HTTP/1.1",
                headerFields: ["Content-Type": "application/json", "Mcp-Session-Id": "fixture-session"])!)
        }
    }

    @main private struct Fixture {
        static func main() async throws {
            var checks: [String: Bool] = [:]
            let config = ServerConfig.resolve(environment: ["NARUMI_HOME": CommandLine.arguments[1]],
                storedRepoPath: nil, bundleURL: nil, fileExists: { _ in false })
            let bootstrap = MCPServerBootstrap(
                serverInstanceID: instance, pid: getpid(), url: config.serverURL,
                certificateSHA256: MCPServerBootstrap.fingerprint(LoopbackTLSCertificate.certificateDER),
                certificatePEM: LoopbackTLSCertificate.certificatePEM,
                tokenAccount: "transport:" + String(repeating: "a", count: 64) + ":" + instance)
            let connection = try MCPServerConnection(bootstrap: bootstrap, token: "fixture-public-token-long-enough-1234")
            func client(
                _ scenario: String, failBootstrap: Bool = false, observer: MCPClient.JobRequestObserver? = nil
            ) -> (MCPClient, FakeHTTP) {
                let http = FakeHTTP(scenario)
                return (MCPClient(config: config, clientVersion: "test", jobRequestObserver: observer,
                    bootstrapLoader: FakeBootstrap(connection: connection, fail: failBootstrap),
                    transportFactory: { _ in http }), http)
            }
            let secretArguments: [String: JSONNode] = ["api_key": .string(fixtureSecret), "request_id": .string(requestID)]
            let (untrusted, noHTTP) = client("normal", failBootstrap: true)
            _ = try? await untrusted.callTool(ToolCatalog.setProviderConnection, arguments: secretArguments)
            checks["bootstrap_before_http"] = noHTTP.allMethods.isEmpty
            let (ordinary, http) = client("normal")
            _ = try await ordinary.callTool(ToolCatalog.listProviders, arguments: [:])
            checks["discovery_before_tools"] = http.allMethods.prefix(2) == ["initialize", "notifications/initialized"]
                && http.calls == [ToolCatalog.getServerInfo, ToolCatalog.listProviders]
            let (v6, v6Wire) = client("v6_success")
            _ = try await v6.callTool(ToolCatalog.listProviders, arguments: [:])
            checks["v6_authenticated_success"] = v6Wire.calls == [
                ToolCatalog.getServerInfo, ToolCatalog.listProviders,
            ]
            for (scenario, key) in [("v1", "v1_rejected"), ("future_contract", "future_contract_rejected"),
                ("missing_tls", "missing_tls_rejected"),
                ("false_auth", "false_client_auth_rejected"), ("wrong_instance", "wrong_instance_rejected")] {
                let (blocked, wire) = client(scenario)
                var failed = false
                do { _ = try await blocked.callTool(ToolCatalog.setProviderConnection, arguments: secretArguments) }
                catch { failed = true }
                checks[key] = failed && wire.count(ToolCatalog.getServerInfo) == 1 && wire.count(ToolCatalog.setProviderConnection) == 0
            }
            let (writer, secretWire) = client("secret404")
            var safeError = false
            do { _ = try await writer.callTool(ToolCatalog.setProviderConnection, arguments: secretArguments) }
            catch { safeError = !String(describing: error).contains(fixtureSecret) }
            for _ in 0..<20 { await writer.recoverPendingJobCalls() }
            checks["secret_not_replayed"] = safeError && secretWire.count(ToolCatalog.setProviderConnection) == 1
            let (setup, setupWire) = client("setup404")
            _ = try? await setup.callTool(ToolCatalog.prepareProviderRuntime, arguments: [
                "provider_id": .string("claude-agent-sdk"), "resource_id": .string("claude-sdk"),
                "request_id": .string(requestID),
            ])
            let pendingBefore = await setup.jobRequests.pendingCount
            for _ in 0..<20 { await setup.recoverPendingJobCalls() }
            let pendingAfter = await setup.jobRequests.pendingCount
            checks["setup_reconciled_without_replay"] = pendingBefore == 1 && pendingAfter == 0
                && setupWire.count(ToolCatalog.prepareProviderRuntime) == 1 && setupWire.count(ToolCatalog.listProviders) == 1
            let (reader, readWire) = client("readonly404")
            _ = try await reader.callTool(ToolCatalog.listProviders, arguments: [:])
            checks["readonly_reconnect"] = readWire.count(ToolCatalog.listProviders) == 2
                && readWire.allMethods.filter { $0 == "initialize" }.count == 2
            for (scenario, key) in [("rpc_error", "rpc_error_redacted"), ("malformed", "malformed_response_redacted")] {
                let (broken, _) = client(scenario)
                do { _ = try await broken.callTool(ToolCatalog.listProviders, arguments: [:]); checks[key] = false }
                catch { checks[key] = !String(describing: error).contains(fixtureSecret) }
            }
            var invalid = config
            invalid.serverURL = URL(string: "http://127.0.0.1:8765/mcp")!
            do { try await ordinary.configure(invalid); checks["invalid_config_rejected"] = false }
            catch { checks["invalid_config_rejected"] = true }
            let unknownFailure = ToolFailure(from: .tool(message: fixtureSecret, payload: .object([
                "error": .object([
                    "code": .string("engine_unavailable"), "message": .string(fixtureSecret),
                    "details": .object([
                        "reason": .string("provider_generation_outcome_unknown"), "outcome_unknown": .bool(true),
                    ]),
                ]),
            ])))
            checks["generation_unknown_localized_without_details"] =
                unknownFailure.code == "engine_unavailable" && unknownFailure.message.contains("結果が不明")
                && unknownFailure.message.contains("自動再送しません") && !unknownFailure.message.contains(fixtureSecret)
                && unknownFailure.providerModelVerification == .unknownOutcome
            let verificationRequest = VerifyProviderModelRequest(
                connectionID: "00000000-0000-4000-8000-000000000010", expectedRevision: 1,
                modelID: "fixture-model", confirmation: .sendTestPromptAndMayCharge)
            for (scenario, key, expected): (String, String, ProviderModelVerificationFailureEvidence?) in [
                ("provider_verify_known", "provider_verification_known_failure_typed", .knownFailure),
                ("provider_verify_unknown", "provider_verification_unknown_outcome_typed", .unknownOutcome),
                ("provider_verify_malformed", "provider_verification_malformed_evidence_blocked", nil),
            ] {
                let (verificationMCP, _) = client(scenario)
                do {
                    _ = try await NarumiClient(mcp: verificationMCP).verifyProviderModel(verificationRequest)
                    checks[key] = false
                } catch let failure as ProviderSettingsFailure {
                    checks[key] = failure.code == .engineUnavailable
                        && failure.modelVerification == expected && !failure.message.contains(fixtureSecret)
                }
            }
            let strippedFailure = providerSettingsFailure(unknownFailure, toolName: ToolCatalog.listProviders)
            checks["provider_verification_evidence_scoped_to_probe_tool"] = strippedFailure.modelVerification == nil
            let selections: [(prefix: String, provider: String, scenario: String, effort: String?, maxTokens: Int?)] = [
                ("v4_codex", "codex-app-server", "minutes_requests", "high", nil),
                ("v4_openai", "openai-api", "minutes_requests", "high", 8192),
                ("v4_anthropic", "anthropic-api", "minutes_requests", nil, 4096),
                ("v4_ollama", "ollama", "minutes_requests", nil, 2048),
                ("v3_codex", "codex-app-server", "codex_minutes_requests", "high", nil),
            ]
            for selection in selections {
                let (minutesMCP, minutesWire) = client(selection.scenario)
                let results = try await checkMinutesSelection(
                    provider: selection.provider, effort: selection.effort, maxTokens: selection.maxTokens,
                    client: NarumiClient(mcp: minutesMCP), wire: minutesWire)
                for (name, passed) in results { checks[selection.prefix + "_" + name] = passed }
            }
            let legacy = MeetingConfig(llmProvider: "none", externalSendPolicy: "local_only")
            let (legacyMCP, legacyWire) = client("legacy_minutes_requests")
            let legacyTyped = NarumiClient(mcp: legacyMCP)
            let legacyUpdates = try NarumiClient.arguments(legacy)
            let savedLegacy = try await legacyTyped.setMeetingConfig(
                meetingID: "20260829T000000Z-a1b2c3d4", scope: nil, updates: legacyUpdates)
            let reloadedLegacy = try await legacyTyped.meeting(id: "20260829T000000Z-a1b2c3d4", scope: nil)
            checks["legacy_meeting_config_omits_new_field"] =
                legacyWire.arguments(ToolCatalog.setMeetingConfig).last?["minutes_model"] == nil
                && legacyWire.arguments(ToolCatalog.setMeetingConfig).last?["transcription_model"] == nil
                && savedLegacy.config == legacy && reloadedLegacy.config == legacy
            let legacyProfile = try await legacyTyped.setProfile(
                name: "fixture-legacy", updates: ["config": .object(legacyUpdates)])
            let reloadedLegacyProfile = try await legacyTyped.profile(name: "fixture-legacy")
            checks["legacy_profile_omits_new_field"] =
                legacyWire.arguments(ToolCatalog.setProfile).last?["config"]?["minutes_model"] == nil
                && legacyWire.arguments(ToolCatalog.setProfile).last?["config"]?["transcription_model"] == nil
                && legacyProfile.config == legacy && reloadedLegacyProfile.config == legacy
            checks["legacy_save_does_not_regenerate"] = legacyWire.count(ToolCatalog.regenerate) == 0
            _ = try await legacyTyped.regenerate(
                meetingID: "20260829T000000Z-a1b2c3d4", scope: nil, force: true, reason: nil, expectedConfig: legacy)
            let legacyGeneration = legacyWire.arguments(ToolCatalog.regenerate).last!
            _ = try await legacyTyped.registerContext(
                meetingID: "20260829T000000Z-a1b2c3d4", scope: nil, sourceType: "text",
                payload: .content("fixture text"), label: nil, autoRegenerate: true, expectedConfig: legacy)
            checks["legacy_generation_omits_new_config_field"] = legacyGeneration["expected_config"] == nil
                && legacyGeneration["force"] == .bool(true)
                && legacyWire.arguments(ToolCatalog.registerContext).last?["expected_config"] == nil
            for (prefix, modelID) in [("v5_whisper", "whisper-1"), ("v5_diarize", "gpt-4o-transcribe-diarize")] {
                let (transcriptionMCP, transcriptionWire) = client("transcription_requests")
                let results = try await checkTranscriptionSelection(
                    modelID: modelID, client: NarumiClient(mcp: transcriptionMCP), wire: transcriptionWire)
                for (name, passed) in results { checks[prefix + "_" + name] = passed }
            }
            let (v4MCP, v4Wire) = client("minutes_requests")
            for (name, passed) in try await checkTranscriptionLegacy(client: NarumiClient(mcp: v4MCP), wire: v4Wire) {
                checks[name] = passed
            }
            for scenario in ["transcription_unknown", "transcription_unknown_invalid"] {
                let (unknownMCP, _) = client(scenario)
                for (name, passed) in try await checkTranscriptionUnknown(
                    client: NarumiClient(mcp: unknownMCP), valid: scenario == "transcription_unknown") {
                    checks[name] = passed
                }
            }
            let (profileUnknownMCP, profileUnknownWire) = client("transcription_profile_save_unknown")
            let (profileRawMCP, _) = client("transcription_profile_save_unknown")
            for (name, passed) in try await checkProfileSaveUnknown(
                client: NarumiClient(mcp: profileUnknownMCP), wire: profileUnknownWire, rawClient: profileRawMCP) {
                checks[name] = passed
            }
            for event in ["unreached", "response_lost", "cancelled", "wrong_meeting", "invalid_receipt"] {
                let (retryMCP, retryWire) = client("transcription_retry_" + event)
                for (name, passed) in try await checkTranscriptionRetryLoss(
                    client: NarumiClient(mcp: retryMCP), wire: retryWire, accepted: event != "unreached") {
                    checks["asr_retry_" + event + "_" + name] = passed
                }
            }
            for code in ["configuration_conflict", "authentication_required", "model_unavailable"] {
                let (rejectedMCP, rejectedWire) = client("transcription_retry_rejected_" + code)
                checks["asr_retry_preflight_" + code] = try await checkTranscriptionRetryRejection(
                    client: NarumiClient(mcp: rejectedMCP), wire: rejectedWire, code: code)
            }
            for (scenario, key, minutes) in [
                ("transcription_ordinary_response_lost", "ordinary_asr_response_loss_recovers", false),
                ("minutes_retry_response_lost", "minutes_response_loss_recovers", true),
            ] {
                let (ordinaryMCP, ordinaryWire) = client(scenario)
                checks[key] = try await checkOrdinaryGenerationRecovery(
                    client: NarumiClient(mcp: ordinaryMCP), wire: ordinaryWire, minutes: minutes)
            }
            for outcome in ["success", "configuration_conflict", "authentication_required", "model_unavailable", "invalid_receipt", "wrong_meeting"] {
                let notifications = FixtureJobNotifications()
                let (manualMCP, manualWire) = client("transcription_manual_" + outcome, observer: { _, pending, _, jobs in
                    notifications.record(pending: pending, jobs: jobs)
                })
                for (name, passed) in try await checkManualTranscriptionRecovery(
                    client: NarumiClient(mcp: manualMCP), wire: manualWire, notifications: notifications, outcome: outcome) {
                    checks["asr_manual_" + outcome + "_" + name] = passed
                }
            }
            print(String(decoding: try JSONEncoder().encode(checks), as: UTF8.self))
        }

        private static func checkMinutesSelection(
            provider: String, effort: String?, maxTokens: Int?, client: NarumiClient, wire: FakeHTTP
        ) async throws -> [String: Bool] {
            var checks: [String: Bool] = [:]
            let policy = provider == "ollama" ? "local_only" : (provider == "codex-app-server" ? "subscription_ok" : "api_ok")
            let selection = MinutesModelSelection(
                provider: provider, connectionID: "conn-0123456789ab", connectionRevision: 4,
                modelID: "fixture-\(provider)-model", reasoningEffort: effort, maxTokens: maxTokens, cacheEpoch: 3)
            let confirmed = MeetingConfig(
                transcriptionEngine: "auto", diarizationEngine: "none", llmProvider: "none",
                externalSendPolicy: policy, language: "ja", selfName: "Fixture Owner", vocabHints: ["fixture vocabulary"],
                minutesModel: selection)
            var parameters: [String: JSONNode] = [:]
            if let effort { parameters["reasoning_effort"] = .string(effort) }
            if let maxTokens { parameters["max_tokens"] = .number(Double(maxTokens)) }
            let confirmedNode = JSONNode.object([
                "transcription_engine": .string("auto"), "diarization_engine": .string("none"),
                "llm_provider": .string("none"), "external_send_policy": .string(policy),
                "language": .string("ja"), "self_name": .string("Fixture Owner"),
                "vocab_hints": .array([.string("fixture vocabulary")]),
                "minutes_model": .object([
                    "provider": .string(provider), "connection_id": .string("conn-0123456789ab"),
                    "connection_revision": .number(4), "model_id": .string("fixture-\(provider)-model"),
                    "parameters": .object(parameters), "cache_epoch": .number(3),
                ]),
            ])
            let saved = try await client.setMeetingConfig(
                meetingID: "20260829T000000Z-a1b2c3d4", scope: "fixture-scope",
                updates: NarumiClient.arguments(confirmed))
            let reloaded = try await client.meeting(id: "20260829T000000Z-a1b2c3d4", scope: "fixture-scope")
            var saveArguments = wire.arguments(ToolCatalog.setMeetingConfig).last!
            let saveRequestID = saveArguments.removeValue(forKey: "request_id")?.stringValue
            let savedMeetingID = saveArguments.removeValue(forKey: "meeting_id")
            let savedScope = saveArguments.removeValue(forKey: "scope")
            checks["meeting_config_round_trip"] = JSONNode.object(saveArguments) == confirmedNode
                && saved.config == confirmed && reloaded.config == confirmed
                && savedMeetingID == .string("20260829T000000Z-a1b2c3d4") && savedScope == .string("fixture-scope")
                && UUID(uuidString: saveRequestID ?? "") != nil
            let profileUpdates: [String: JSONNode] = [
                "config": .object(try NarumiClient.arguments(confirmed)), "scope": .string("fixture-scope"),
                "engagement": .string("fixture-engagement"), "export_destinations": .array([.string("markdown")]),
                "make_default": .bool(false),
            ]
            let savedProfile = try await client.setProfile(name: "fixture-\(provider)", updates: profileUpdates)
            let reloadedProfile = try await client.profile(name: "fixture-\(provider)")
            var profileArguments = wire.arguments(ToolCatalog.setProfile).last!
            let profileRequestID = profileArguments.removeValue(forKey: "request_id")?.stringValue
            let profileName = profileArguments.removeValue(forKey: "name")
            checks["profile_round_trip"] = profileArguments["config"] == confirmedNode
                && profileArguments == profileUpdates && savedProfile.config == confirmed
                && savedProfile == reloadedProfile && profileName == .string("fixture-\(provider)")
                && UUID(uuidString: profileRequestID ?? "") != nil && profileRequestID != saveRequestID
            checks["save_does_not_regenerate"] = wire.count(ToolCatalog.regenerate) == 0
                && wire.count(ToolCatalog.registerContext) == 0
            _ = try await client.regenerate(
                meetingID: "20260829T000000Z-a1b2c3d4", scope: "fixture-scope", force: false,
                reason: "fixture reason", expectedConfig: confirmed)
            let generationArguments = wire.arguments(ToolCatalog.regenerate).last!
            checks["expected_config_sent"] = generationArguments["expected_config"] == confirmedNode
                && generationArguments["scope"] == .string("fixture-scope")
                && generationArguments["reason"] == .string("fixture reason") && generationArguments["force"] == nil
            var forceRejected = false
            do {
                _ = try await client.regenerate(
                    meetingID: "20260829T000000Z-a1b2c3d4", scope: nil, force: true,
                    reason: nil, expectedConfig: confirmed)
            } catch let failure as ToolFailure {
                forceRejected = failure.code == "invalid_argument"
            }
            checks["force_rejected"] = forceRejected && wire.count(ToolCatalog.regenerate) == 1
            _ = try await client.registerContext(
                meetingID: "20260829T000000Z-a1b2c3d4", scope: nil, sourceType: "text",
                payload: .content("fixture text"), label: nil, autoRegenerate: true, expectedConfig: confirmed)
            let contextArguments = wire.arguments(ToolCatalog.registerContext).last!
            checks["context_expected_config_sent"] = contextArguments["expected_config"] == confirmedNode
                && contextArguments["auto_regenerate"] == .bool(true)
            _ = try await client.registerContext(
                meetingID: "20260829T000000Z-a1b2c3d4", scope: nil, sourceType: "text",
                payload: .content("fixture text"), label: nil, autoRegenerate: false, expectedConfig: confirmed)
            let nonGenerating = wire.arguments(ToolCatalog.registerContext).last!
            checks["non_generating_context_omits_config"] = nonGenerating["expected_config"] == nil
                && nonGenerating["auto_regenerate"] == nil
            return checks
        }
    }
    """#
}
