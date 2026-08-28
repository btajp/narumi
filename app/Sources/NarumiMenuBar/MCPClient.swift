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
        case .httpStatus(let status, let body): return "HTTP \(status): \(body)"
        case .protocolError(let message): return "プロトコルエラー: \(message)"
        case .rpc(let code, let message): return "JSON-RPC エラー \(code): \(message)"
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

    let serverURL: URL
    private let clientVersion: String
    private let transport: MCPHTTPTransport
    private var sessionID: String?
    private var initialized = false
    private var permissionSession = MCPPermissionSessionState()
    private var nextID = 1
    var jobRequests = DesktopJobRequestState()
    let jobRequestObserver: JobRequestObserver?
    var jobRequestPublication: UInt64 = 0
    var unpublishedJobIDs: [String: UInt64] = [:]

    init(serverURL: URL, clientVersion: String, jobRequestObserver: JobRequestObserver? = nil) {
        self.serverURL = serverURL
        self.clientVersion = clientVersion
        self.jobRequestObserver = jobRequestObserver
        transport = MCPHTTPTransport()
    }

    static func serverURLFromEnvironment() -> URL {
        let fallback = URL(string: "http://127.0.0.1:8765/mcp")!
        guard let raw = ProcessInfo.processInfo.environment["NARUMI_SERVER_URL"], !raw.isEmpty else {
            return fallback
        }
        return URL(string: raw) ?? fallback
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
            throw MCPClientError.tool(message: MCPClient.errorMessage(from: callResult), payload: structured)
        }
        if name == ToolCatalog.getServerInfo {
            permissionSession.observeServerInfo(
                contractVersion: structured?["contract_version"]?.stringValue,
                serverInstanceID: structured?["server_instance_id"]?.stringValue,
                requestGeneration: responseGeneration)
            callResult.sessionGeneration = responseGeneration == permissionSession.generation ? responseGeneration : nil
        }
        return callResult
    }

    /// Drop the session so the next call re-initializes (used after connection failures).
    func reset() {
        sessionID = nil
        initialized = false
        permissionSession.reset()
    }

    static func errorMessage(from result: ToolCallResult) -> String {
        if let error = result.structuredContent?["error"] {
            let code = error["code"]?.stringValue ?? "error"
            let message = error["message"]?.stringValue ?? ""
            return "\(code): \(message)"
        }
        return result.text.isEmpty ? "ツールがエラーを返しました" : result.text
    }

    // MARK: Session

    private func ensureInitialized(confidential: Bool) async throws {
        if initialized {
            return
        }
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
        let message = try MCPClient.extractResponse(data: data, response: response, expectedID: id)
        if let error = message["error"] {
            let code = Int(error["code"].flatMap { node -> Double? in
                if case .number(let value) = node { return value }
                return nil
            } ?? -1)
            throw MCPClientError.rpc(code: code, message: error["message"]?.stringValue ?? "unknown error")
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
                throw MCPClientError.protocolError("接続先の権限設定機能を再確認する必要があります。診断から状態を再確認してください。")
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
        do {
            (data, response) = try await transport.data(for: request, protectingSecrets: confidential)
        } catch {
            throw MCPClientError.transport(error.localizedDescription)
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
            let text = String(data: data, encoding: .utf8) ?? ""
            throw MCPClientError.httpStatus(http.statusCode, text)
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
            if case .number(let id)? = candidate["id"], Int(id) == expectedID {
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
