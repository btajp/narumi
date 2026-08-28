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
        init(_ scenario: String) { self.scenario = scenario }
        func invalidate() {}
        func count(_ tool: String) -> Int { lock.withLock { tools.filter { $0 == tool }.count } }
        var calls: [String] { lock.withLock { tools } }
        var allMethods: [String] { lock.withLock { methods } }
        func arguments(_ tool: String) -> [[String: JSONNode]] { lock.withLock { argumentsByTool[tool] ?? [] } }

        func data(for request: URLRequest, protectingSecrets: Bool) async throws -> (Data, URLResponse) {
            let rpc = try JSONNode.parse(request.httpBody!)
            let method = rpc["method"]!.stringValue!
            lock.withLock { methods.append(method) }
            if method == "notifications/initialized" { return response(request, status: 202, body: Data()) }
            let id = rpc["id"]!
            if method == "initialize" { return try result(request, id: id, value: .object([:])) }
            let name = rpc["params"]!["name"]!.stringValue!
            lock.withLock { tools.append(name) }
            if let value = rpc["params"]?["arguments"], case .object(let arguments) = value {
                lock.withLock { argumentsByTool[name, default: []].append(arguments) }
            }
            if name == ToolCatalog.getServerInfo {
                var info: [String: JSONNode] = [
                    "contract_version": .string(scenario == "v1" ? "1.1.0" : (scenario == "minutes_requests" ? "3.0.0" : "2.0.0")),
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
            if scenario == "minutes_requests" || scenario == "legacy_minutes_requests" {
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
            func client(_ scenario: String, failBootstrap: Bool = false) -> (MCPClient, FakeHTTP) {
                let http = FakeHTTP(scenario)
                return (MCPClient(config: config, clientVersion: "test",
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
            for (scenario, key) in [("v1", "v1_rejected"), ("missing_tls", "missing_tls_rejected"),
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
            let (minutesMCP, minutesWire) = client("minutes_requests")
            let typed = NarumiClient(mcp: minutesMCP)
            let confirmed = MeetingConfig(
                transcriptionEngine: "auto", diarizationEngine: "none", llmProvider: "none",
                externalSendPolicy: "subscription_ok", language: "ja",
                minutesModel: CodexMinutesSelection(
                    connectionID: "conn-0123456789ab", connectionRevision: 4, modelID: "fixture-codex-model",
                    reasoningEffort: "high", cacheEpoch: 3))
            let confirmedNode = JSONNode.object(try NarumiClient.arguments(confirmed))
            _ = try await typed.regenerate(
                meetingID: "20260829T000000Z-a1b2c3d4", scope: "fixture-scope", force: false,
                reason: "fixture reason", expectedConfig: confirmed)
            let generationArguments = minutesWire.arguments(ToolCatalog.regenerate).last!
            checks["codex_expected_config_sent"] = generationArguments["expected_config"] == confirmedNode
                && generationArguments["scope"] == .string("fixture-scope") && generationArguments["force"] == nil
            var forceRejected = false
            do {
                _ = try await typed.regenerate(
                    meetingID: "20260829T000000Z-a1b2c3d4", scope: nil, force: true,
                    reason: nil, expectedConfig: confirmed)
            } catch { forceRejected = true }
            checks["codex_force_rejected"] = forceRejected && minutesWire.count(ToolCatalog.regenerate) == 1
            _ = try await typed.registerContext(
                meetingID: "20260829T000000Z-a1b2c3d4", scope: nil, sourceType: "text",
                payload: .content("fixture text"), label: nil, autoRegenerate: true, expectedConfig: confirmed)
            let contextArguments = minutesWire.arguments(ToolCatalog.registerContext).last!
            checks["context_expected_config_sent"] = contextArguments["expected_config"] == confirmedNode
                && contextArguments["auto_regenerate"] == .bool(true)
            _ = try await typed.registerContext(
                meetingID: "20260829T000000Z-a1b2c3d4", scope: nil, sourceType: "text",
                payload: .content("fixture text"), label: nil, autoRegenerate: false, expectedConfig: confirmed)
            let nonGenerating = minutesWire.arguments(ToolCatalog.registerContext).last!
            checks["non_generating_context_omits_config"] = nonGenerating["expected_config"] == nil
                && nonGenerating["auto_regenerate"] == nil
            let legacy = MeetingConfig(llmProvider: "none", externalSendPolicy: "local_only")
            let (legacyMCP, legacyWire) = client("legacy_minutes_requests")
            let legacyTyped = NarumiClient(mcp: legacyMCP)
            _ = try await legacyTyped.regenerate(
                meetingID: "20260829T000000Z-a1b2c3d4", scope: nil, force: true, reason: nil, expectedConfig: legacy)
            let legacyGeneration = legacyWire.arguments(ToolCatalog.regenerate).last!
            _ = try await legacyTyped.registerContext(
                meetingID: "20260829T000000Z-a1b2c3d4", scope: nil, sourceType: "text",
                payload: .content("fixture text"), label: nil, autoRegenerate: true, expectedConfig: legacy)
            checks["legacy_generation_omits_new_config_field"] = legacyGeneration["expected_config"] == nil
                && legacyGeneration["force"] == .bool(true)
                && legacyWire.arguments(ToolCatalog.registerContext).last?["expected_config"] == nil
            print(String(decoding: try JSONEncoder().encode(checks), as: UTF8.self))
        }
    }
    """#
}
