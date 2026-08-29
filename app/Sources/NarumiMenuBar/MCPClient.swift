import Foundation
import NarumiMenuBarCore

enum MCPClientError: Error, CustomStringConvertible {
    case transport(String)
    case httpStatus(Int, String)
    case protocolError(String)
    case rpc(code: Int, message: String)
    /// `isError: true` from `tools/call`; `payload` is the structured `{"error": ...}` when present.
    case tool(message: String, payload: JSONNode?)

    var description: String {
        switch self {
        case .transport(let message): return "接続できません: \(message)"
        case .httpStatus(let status, _): return "HTTP \(status): サーバー要求を完了できませんでした"
        case .protocolError(let message): return "プロトコルエラー: \(message)"
        case .rpc(let code, _): return "JSON-RPC エラー \(code): サーバー要求を完了できませんでした"
        case .tool(let message, _): return message
        }
    }
}

struct ToolCallResult: Sendable {
    let structuredContent: JSONNode?
    let text: String
    let isError: Bool
    var sessionGeneration: UInt64? = nil
}

/// Minimal MCP Streamable HTTP client (JSON-RPC 2.0 over POST) for the narumi server.
///
/// Handles `initialize` → `notifications/initialized` once, keeps `Mcp-Session-Id`, and accepts
/// both `application/json` and `text/event-stream` responses.
actor MCPClient {
    typealias JobRequestObserver = @MainActor @Sendable (UInt64, Int, Bool, Set<String>) -> Void
    static let protocolVersion = "2025-06-18"
    static let clientName = "narumi-menubar"

    var config: ServerConfig
    var serverURL: URL { config.serverURL }
    var bootstrap: MCPServerBootstrap?
    var transport: (any MCPHTTPTransporting)?
    let bootstrapLoader: (any MCPServerBootstrapLoading)?
    let transportFactory: @Sendable (MCPServerConnection) -> any MCPHTTPTransporting
    private let clientVersion: String
    private var sessionID: String?
    private var initialized = false
    private var initializationTask: Task<Void, any Error>?
    private var permissionSession = MCPPermissionSessionState()
    private var nextID = 1
    var jobRequests = DesktopJobRequestState()
    let jobRequestObserver: JobRequestObserver?
    var jobRequestPublication: UInt64 = 0
    var unpublishedJobIDs: [String: UInt64] = [:]
    var operationSessionGeneration: UInt64 { permissionSession.generation }
    var negotiatedContractVersion: String? { permissionSession.contractVersion }

    func contractVersionForEncoding() async throws -> String? {
        if permissionSession.contractVersion == nil {
            _ = try await performToolCall(ToolCatalog.getServerInfo, arguments: [:], confidential: false)
        }
        return permissionSession.contractVersion
    }

    init(
        config: ServerConfig, clientVersion: String, jobRequestObserver: JobRequestObserver? = nil,
        bootstrapLoader: (any MCPServerBootstrapLoading)? = nil,
        transportFactory: @escaping @Sendable (MCPServerConnection) -> any MCPHTTPTransporting = {
            MCPHTTPTransport(connection: $0)
        }
    ) {
        self.config = config
        self.clientVersion = clientVersion
        self.jobRequestObserver = jobRequestObserver
        self.bootstrapLoader = bootstrapLoader
        self.transportFactory = transportFactory
    }

    // MARK: Public API

    /// `tools/call`. Throws `MCPClientError.tool` when the server reports `isError`.
    func callTool(
        _ name: String, arguments: [String: JSONNode], expectedSessionGeneration: UInt64? = nil
    ) async throws -> ToolCallResult {
        let confidential = MCPHTTPTransport.isConfidentialTool(name)
        do {
            if confidential {
                _ = try MCPHTTPTransport.confidentialEndpoint(serverURL)
            }
            if name == ToolCatalog.configureRecordingPermission
                || (name == ToolCatalog.getServerInfo && arguments["refresh_permissions"]?.boolValue == true) {
                return try await performToolCall(
                    name, arguments: arguments, confidential: confidential,
                    expectedSessionGeneration: expectedSessionGeneration)
            }
            return try await performJobTrackedToolCall(name, arguments: arguments, confidential: confidential)
        } catch {
            guard confidential else { throw error }
            // Servers and URLSession errors may reflect the write-only input. Keep only a
            // known contract code (notably invalid_argument for the sheet's recovery flow).
            var code: String?
            if case MCPClientError.tool(_, let payload) = error {
                code = payload?["error"]?["code"]?.stringValue
            }
            let message = MCPHTTPTransport.confidentialErrorMessage
            throw MCPClientError.tool(
                message: message,
                payload: .object(["error": .object([
                    "code": .string(MCPHTTPTransport.confidentialErrorCode(code)),
                    "message": .string(message),
                ])]))
        }
    }

    func performToolCall(
        _ name: String, arguments: [String: JSONNode], confidential: Bool,
        expectedSessionGeneration: UInt64? = nil
    ) async throws -> ToolCallResult {
        try await ensureInitialized(confidential: confidential)
        if name != ToolCatalog.getServerInfo, permissionSession.contractVersion == nil {
            _ = try await performToolCall(ToolCatalog.getServerInfo, arguments: [:], confidential: false)
        }
        try Task.checkCancellation()
        let params: JSONNode = .object(["name": .string(name), "arguments": .object(arguments)])
        let result: JSONNode
        var responseGeneration = permissionSession.generation
        do {
            result = try await request(
                method: "tools/call", params: params, confidential: confidential,
                expectedSessionGeneration: expectedSessionGeneration)
        } catch MCPClientError.httpStatus(404, let body) {
            // A permission mutation can outlive a lost session/response. Reconnect only
            // for later read-only reconciliation, never replay its OS action here.
            reset()
            guard MCPToolReplayPolicy.allowsSessionRetry(
                tool: name, refreshingPermissions: arguments["refresh_permissions"]?.boolValue == true
            ) else {
                throw MCPClientError.httpStatus(404, body)
            }
            try await ensureInitialized(confidential: confidential)
            if name != ToolCatalog.getServerInfo {
                _ = try await performToolCall(ToolCatalog.getServerInfo, arguments: [:], confidential: false)
            }
            responseGeneration = permissionSession.generation
            do {
                result = try await request(method: "tools/call", params: params, confidential: confidential)
            } catch {
                throw MCPClientError.httpStatus(404, body)
            }
        }
        let structured = result["structuredContent"]
        let isError = result["isError"]?.boolValue ?? false
        var texts: [String] = []
        if case .array(let contents)? = result["content"] {
            for content in contents {
                if content["type"]?.stringValue == "text", let text = content["text"]?.stringValue {
                    texts.append(text)
                }
            }
        }
        let text = texts.joined(separator: "\n")
        var callResult = ToolCallResult(
            structuredContent: structured, text: text, isError: isError,
            sessionGeneration: responseGeneration == permissionSession.generation ? responseGeneration : nil)
        if isError {
            var message = MCPClient.errorMessage(from: callResult)
            let code = MCPHTTPTransport.confidentialErrorCode(structured?["error"]?["code"]?.stringValue)
            var safeError: [String: JSONNode] = ["code": .string(code)]
            if !confidential, let details = structured?["error"]?["details"] {
                message = ToolErrorInfo.generationOutcomeMessage(
                    reason: details["reason"]?.stringValue,
                    unknown: details["outcome_unknown"]?.boolValue == true,
                    stage: details["stage"]?.stringValue) ?? message
                // Preserve only the closed, bounded evidence needed for explicit ASR review.
                // Untrusted error text, arbitrary details and confidential-tool payloads stay redacted.
                if let data = try? details.serialized(),
                    let outcome = try? JSONDecoder().decode(TranscriptionOutcomeUnknownDetails.self, from: data),
                    let encoded = try? JSONEncoder().encode(outcome), let safeDetails = try? JSONNode.parse(encoded) {
                    safeError["details"] = safeDetails
                }
            }
            safeError["message"] = .string(message)
            throw MCPClientError.tool(message: message, payload: .object(["error": .object(safeError)]))
        }
        if name == ToolCatalog.getServerInfo {
            guard structured?["server_instance_id"]?.stringValue == bootstrap?.serverInstanceID else {
                reset()
                throw MCPConnectionError.connectionChanged
            }
            guard RecordingPermissionContract.supportsSetup(structured?["contract_version"]?.stringValue),
                structured?["secure_transport"]?["mode"]?.stringValue == "pinned_tls",
                structured?["secure_transport"]?["tls_required"]?.boolValue == true,
                structured?["secure_transport"]?["client_auth_required"]?.boolValue == true
            else {
                reset()
                throw MCPConnectionError.incompatibleContract
            }
            permissionSession.observeServerInfo(
                contractVersion: structured?["contract_version"]?.stringValue,
                serverInstanceID: structured?["server_instance_id"]?.stringValue,
                requestGeneration: responseGeneration)
            callResult.sessionGeneration = responseGeneration == permissionSession.generation ? responseGeneration : nil
        }
        if name == ToolCatalog.listProviders {
            await reconcileProviderSetupRequests(callResult)
        }
        return callResult
    }

    /// Drop the session so the next call re-initializes (used after connection failures).
    func reset() {
        initializationTask?.cancel()
        initializationTask = nil
        transport?.invalidate()
        transport = nil
        bootstrap = nil
        sessionID = nil
        initialized = false
        permissionSession.reset()
        jobRequests.invalidateRetries()
    }

    static func errorMessage(from result: ToolCallResult) -> String {
        let code = MCPHTTPTransport.confidentialErrorCode(result.structuredContent?["error"]?["code"]?.stringValue)
        return "\(code): 操作を完了できませんでした。設定と接続状態を確認してください。"
    }

    // MARK: Session

    private func ensureInitialized(confidential: Bool) async throws {
        if initialized { return }
        if let initializationTask {
            try await initializationTask.value
            return
        }
        let task = Task { try await initializeSession(confidential: confidential) }
        initializationTask = task
        defer { if initializationTask == task { initializationTask = nil } }
        try await task.value
    }

    private func initializeSession(confidential: Bool) async throws {
        try prepareConnection()
        let generation = permissionSession.generation
        let params: JSONNode = .object([
            "protocolVersion": .string(MCPClient.protocolVersion),
            "capabilities": .object([:]),
            "clientInfo": .object([
                "name": .string(MCPClient.clientName),
                "version": .string(clientVersion),
            ]),
        ])
        _ = try await request(method: "initialize", params: params, confidential: confidential)
        try await notify(method: "notifications/initialized", confidential: confidential)
        guard generation == permissionSession.generation else { throw MCPConnectionError.connectionChanged }
        initialized = true
    }

    // MARK: Transport

    private func request(
        method: String, params: JSONNode, confidential: Bool, expectedSessionGeneration: UInt64? = nil
    ) async throws -> JSONNode {
        let id = nextID
        nextID += 1
        let body: JSONNode = .object([
            "jsonrpc": .string("2.0"),
            "id": .number(Double(id)),
            "method": .string(method),
            "params": params,
        ])
        let (data, response) = try await post(
            body, confidential: confidential, expectedSessionGeneration: expectedSessionGeneration)
        let message: JSONNode
        do {
            message = try MCPClient.extractResponse(data: data, response: response, expectedID: id)
        } catch {
            throw MCPClientError.protocolError("サーバー応答を解釈できませんでした。")
        }
        if let error = message["error"] {
            let code = error["code"].flatMap { node -> Int? in
                if case .number(let value) = node { return Int(exactly: value) }
                return nil
            } ?? -1
            throw MCPClientError.rpc(code: code, message: "サーバー要求を完了できませんでした。")
        }
        guard let result = message["result"] else {
            throw MCPClientError.protocolError("response without result for \(method)")
        }
        return result
    }

    private func notify(method: String, confidential: Bool) async throws {
        let body: JSONNode = .object([
            "jsonrpc": .string("2.0"),
            "method": .string(method),
        ])
        _ = try await post(body, confidential: confidential)
    }

    private func post(
        _ body: JSONNode, confidential: Bool, expectedSessionGeneration: UInt64? = nil
    ) async throws -> (Data, HTTPURLResponse) {
        if let expectedSessionGeneration, expectedSessionGeneration != permissionSession.generation {
            throw MCPClientError.protocolError("権限操作の確認後に接続が変わりました。診断から状態を再確認してください。")
        }
        if body["method"]?.stringValue == "tools/call", let tool = body["params"]?["name"]?.stringValue {
            let refreshing = body["params"]?["arguments"]?["refresh_permissions"]?.boolValue == true
            guard (tool != ToolCatalog.configureRecordingPermission || expectedSessionGeneration != nil),
                permissionSession.allowsCall(tool: tool, refreshingPermissions: refreshing) else {
                throw MCPConnectionError.incompatibleContract
            }
        }
        let requestGeneration = permissionSession.generation
        var request = URLRequest(url: serverURL)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue("application/json, text/event-stream", forHTTPHeaderField: "Accept")
        request.setValue(MCPClient.protocolVersion, forHTTPHeaderField: "MCP-Protocol-Version")
        if let sessionID {
            request.setValue(sessionID, forHTTPHeaderField: "Mcp-Session-Id")
        }
        request.httpBody = try body.serialized()

        let data: Data
        let response: URLResponse
        guard let transport else { throw MCPConnectionError.bootstrapUnavailable }
        do {
            (data, response) = try await transport.data(for: request, protectingSecrets: confidential)
        } catch let error as MCPConnectionError {
            throw error
        } catch is CancellationError {
            throw CancellationError()
        } catch {
            throw MCPConnectionError.transportFailed
        }
        guard let http = response as? HTTPURLResponse else {
            throw MCPClientError.protocolError("non-HTTP response")
        }
        guard permissionSession.generation == requestGeneration else {
            throw MCPClientError.protocolError("接続が切り替わったため、以前の応答を破棄しました。")
        }
        if let newSessionID = http.value(forHTTPHeaderField: "Mcp-Session-Id"), !newSessionID.isEmpty {
            if let sessionID, sessionID != newSessionID {
                permissionSession.reset()
            }
            sessionID = newSessionID
        }
        guard (200..<300).contains(http.statusCode) else {
            throw MCPClientError.httpStatus(http.statusCode, "")
        }
        return (data, http)
    }

    /// Pick the JSON-RPC response with `expectedID` from a JSON body or an SSE stream.
    static func extractResponse(data: Data, response: HTTPURLResponse, expectedID: Int) throws -> JSONNode {
        let contentType = response.value(forHTTPHeaderField: "Content-Type")?.lowercased() ?? ""
        let candidates: [JSONNode]
        if contentType.contains("text/event-stream") {
            candidates = try parseSSE(data).map(JSONNode.parse)
        } else {
            let node = try JSONNode.parse(data)
            if case .array(let batch) = node {
                candidates = batch
            } else {
                candidates = [node]
            }
        }
        for candidate in candidates {
            if case .number(let id)? = candidate["id"], id == Double(expectedID) {
                return candidate
            }
        }
        throw MCPClientError.protocolError("no response with id \(expectedID)")
    }

    /// `data:` payloads of every SSE event in the body (multi-line data joined with `\n`).
    static func parseSSE(_ data: Data) throws -> [Data] {
        guard let text = String(data: data, encoding: .utf8) else {
            throw MCPClientError.protocolError("SSE body is not UTF-8")
        }
        var events: [Data] = []
        var current: [String] = []
        func flush() {
            if !current.isEmpty {
                let joined = current.joined(separator: "\n")
                if !joined.trimmingCharacters(in: .whitespaces).isEmpty {
                    events.append(Data(joined.utf8))
                }
                current.removeAll()
            }
        }
        for rawLine in text.split(omittingEmptySubsequences: false, whereSeparator: { $0 == "\n" || $0 == "\r\n" }) {
            let line = String(rawLine)
            if line.isEmpty {
                flush()
                continue
            }
            if line.hasPrefix(":") {
                continue
            }
            if line.hasPrefix("data:") {
                var value = String(line.dropFirst("data:".count))
                if value.hasPrefix(" ") {
                    value.removeFirst()
                }
                current.append(value)
            }
        }
        flush()
        return events
    }
}
