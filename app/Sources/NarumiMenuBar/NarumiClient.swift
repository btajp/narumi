import Foundation
import NarumiMenuBarCore

/// Uniform failure the window presents: contract error code + message.
///
/// `code` is the `error_envelope` code (`busy`, `not_found`, …) when the server returned one,
/// otherwise a transport-level pseudo-code. `busy` is surfaced as a non-modal toast.
struct ToolFailure: Error, Equatable, CustomStringConvertible {
    var code: String
    var message: String

    var isBusy: Bool { code == "busy" }
    var description: String { "\(code): \(message)" }

    init(code: String, message: String) {
        self.code = code
        self.message = message
    }

    init(from error: MCPClientError) {
        switch error {
        case .tool(let message, let payload):
            if let inner = payload?["error"], let code = inner["code"]?.stringValue {
                self.code = code
                self.message = inner["message"]?.stringValue ?? message
            } else {
                self.code = "error"
                self.message = message
            }
        case .transport(let message):
            self.code = "transport"
            self.message = "接続できません: \(message)"
        case .httpStatus(let status, let body):
            self.code = "transport"
            self.message = "HTTP \(status): \(body)"
        case .protocolError(let message):
            self.code = "protocol"
            self.message = message
        case .rpc(let code, let message):
            self.code = "protocol"
            self.message = "JSON-RPC \(code): \(message)"
        }
    }
}

/// Thin typed layer over `MCPClient`: one async func per tool the window uses, returning the
/// decoded contract model from `NarumiMenuBarCore`. Every write call issues a fresh
/// `request_id` (UUID). Tool names come from `ToolCatalog` only (surface parity).
struct NarumiClient: Sendable {
    let mcp: MCPClient

    init(mcp: MCPClient) {
        self.mcp = mcp
    }

    // MARK: Plumbing

    func call<T: Decodable>(
        _ name: String, _ arguments: [String: JSONNode] = [:], expectedSessionGeneration: UInt64? = nil
    ) async throws -> T {
        let result: ToolCallResult
        do {
            result = try await mcp.callTool(
                name, arguments: arguments, expectedSessionGeneration: expectedSessionGeneration)
        } catch let error as MCPClientError {
            throw ToolFailure(from: error)
        }
        guard let structured = result.structuredContent else {
            throw ToolFailure(code: "protocol", message: "\(name) が structuredContent を返しません")
        }
        do {
            let data = try structured.serialized()
            return try JSONDecoder().decode(T.self, from: data)
        } catch let error as MCPClientError {
            throw ToolFailure(from: error)
        } catch {
            throw ToolFailure(
                code: "protocol",
                message: ToolResponseErrorMessage.decoding(toolName: name, error: error))
        }
    }

    private static func requestID() -> JSONNode {
        .string(UUID().uuidString)
    }

    static func arguments<T: Encodable>(_ request: T) throws -> [String: JSONNode] {
        let data = try JSONEncoder().encode(request)
        guard case .object(let arguments) = try JSONNode.parse(data) else {
            throw ToolFailure(code: "protocol", message: "ツールの入力が JSON オブジェクトではありません")
        }
        return arguments
    }

    /// Scope selector: empty = omit (unscoped only), one name = string, 2+ = explicit array.
    private static func scopeNode(_ scopes: [String]) -> JSONNode? {
        switch scopes.count {
        case 0: return nil
        case 1: return .string(scopes[0])
        default: return .array(scopes.map(JSONNode.string))
        }
    }

    // MARK: Meetings

    func listMeetings(query: String? = nil, scope: [String] = [], limit: Int? = nil) async throws -> [MeetingSummary] {
        var args: [String: JSONNode] = [:]
        if let query, !query.isEmpty {
            args["query"] = .string(query)
        }
        if let scope = Self.scopeNode(scope) {
            args["scope"] = scope
        }
        if let limit {
            args["limit"] = .number(Double(limit))
        }
        let response: ListMeetingsResponse = try await call(ToolCatalog.listMeetings, args)
        return response.meetings
    }

    func searchTranscripts(query: String, scope: [String] = [], limit: Int? = nil) async throws -> [SearchHit] {
        var args: [String: JSONNode] = ["query": .string(query)]
        if let scope = Self.scopeNode(scope) {
            args["scope"] = scope
        }
        if let limit {
            args["limit"] = .number(Double(limit))
        }
        let response: SearchTranscriptsResponse = try await call(ToolCatalog.searchTranscripts, args)
        return response.hits
    }

    func meeting(id: String, scope: String?) async throws -> MeetingDetail {
        var args: [String: JSONNode] = ["meeting_id": .string(id)]
        if let scope {
            args["scope"] = .string(scope)
        }
        return try await call(ToolCatalog.getMeeting, args)
    }

    // MARK: Recording

    func recordingStatus() async throws -> RecordingStatus {
        try await call(ToolCatalog.getRecordingStatus)
    }

    /// Stop from the window banner; the server enqueues the process job (auto_process default).
    func stopRecording() async throws {
        do {
            _ = try await mcp.callTool(ToolCatalog.stopRecording, arguments: ["request_id": Self.requestID()])
        } catch let error as MCPClientError {
            throw ToolFailure(from: error)
        }
    }

    // MARK: Minutes / transcript

    func minutes(meetingID: String, version: Int? = nil, scope: String?) async throws -> Minutes {
        var args: [String: JSONNode] = ["meeting_id": .string(meetingID)]
        if let version {
            args["version"] = .number(Double(version))
        }
        if let scope {
            args["scope"] = .string(scope)
        }
        return try await call(ToolCatalog.getMinutes, args)
    }

    func transcript(meetingID: String, source: String? = nil, scope: String?) async throws -> Transcript {
        var args: [String: JSONNode] = ["meeting_id": .string(meetingID)]
        if let source {
            args["source"] = .string(source)
        }
        if let scope {
            args["scope"] = .string(scope)
        }
        return try await call(ToolCatalog.getTranscript, args)
    }

    func regenerate(
        meetingID: String, scope: String?, force: Bool, reason: String?, expectedConfig: MeetingConfig? = nil
    ) async throws -> RegenerateResponse {
        guard !force || expectedConfig?.minutesModel == nil else {
            throw ToolFailure(code: "invalid_argument", message: "Codex の新しい試行は会議設定で試行番号を増やして保存してください。")
        }
        var args: [String: JSONNode] = [
            "meeting_id": .string(meetingID),
            "request_id": Self.requestID(),
        ]
        if let scope {
            args["scope"] = .string(scope)
        }
        if force {
            args["force"] = .bool(true)
        }
        if let reason, !reason.isEmpty {
            args["reason"] = .string(reason)
        }
        // Contract 2 servers have no expected_config. A contract 3 server rejects a concurrent
        // legacy -> Codex switch because its saved Codex config requires this confirmation.
        if let expectedConfig, expectedConfig.minutesModel != nil {
            args["expected_config"] = .object(try Self.arguments(expectedConfig))
        }
        return try await call(ToolCatalog.regenerate, args)
    }

    // MARK: Context

    enum ContextPayload {
        case content(String)
        case url(String)
        case filePath(String)
    }

    func registerContext(
        meetingID: String, scope: String?, sourceType: String, payload: ContextPayload,
        label: String?, autoRegenerate: Bool, expectedConfig: MeetingConfig? = nil
    ) async throws -> RegisterContextResponse {
        var args: [String: JSONNode] = [
            "meeting_id": .string(meetingID),
            "source_type": .string(sourceType),
            "request_id": Self.requestID(),
        ]
        if let scope {
            args["scope"] = .string(scope)
        }
        switch payload {
        case .content(let text): args["content"] = .string(text)
        case .url(let url): args["url"] = .string(url)
        case .filePath(let path): args["file_path"] = .string(path)
        }
        if let label, !label.isEmpty {
            args["label"] = .string(label)
        }
        if autoRegenerate {
            args["auto_regenerate"] = .bool(true)
            if let expectedConfig, expectedConfig.minutesModel != nil {
                args["expected_config"] = .object(try Self.arguments(expectedConfig))
            }
        }
        return try await call(ToolCatalog.registerContext, args)
    }

    // MARK: Config

    /// `updates` carries only the keys to change (set_meeting_config semantics); the meeting id,
    /// scope selector and request_id are added here.
    func setMeetingConfig(meetingID: String, scope: String?, updates: [String: JSONNode]) async throws -> SetMeetingConfigResponse {
        var args = updates
        args["meeting_id"] = .string(meetingID)
        args["request_id"] = Self.requestID()
        if let scope {
            args["scope"] = .string(scope)
        }
        return try await call(ToolCatalog.setMeetingConfig, args)
    }

    // MARK: Export

    func exportDestinations() async throws -> [ExportDestinationInfo] {
        let response: ListExportDestinationsResponse = try await call(ToolCatalog.listExportDestinations)
        return response.destinations
    }

    func exportMinutes(
        meetingID: String, scope: String?, destination: String,
        options: [String: JSONNode]?, minutesVersion: Int?
    ) async throws -> ExportMinutesResponse {
        var args: [String: JSONNode] = [
            "meeting_id": .string(meetingID),
            "destination": .string(destination),
            "request_id": Self.requestID(),
        ]
        if let scope {
            args["scope"] = .string(scope)
        }
        if let options, !options.isEmpty {
            args["options"] = .object(options)
        }
        if let minutesVersion {
            args["minutes_version"] = .number(Double(minutesVersion))
        }
        return try await call(ToolCatalog.exportMinutes, args)
    }

    // MARK: Jobs

    func jobStatus(jobID: String) async throws -> Job {
        let response: JobStatusResponse = try await call(ToolCatalog.getJobStatus, ["job_id": .string(jobID)])
        return response.job
    }

    func cancelJob(jobID: String) async throws -> Job {
        let response: JobStatusResponse = try await call(
            ToolCatalog.cancelJob, ["job_id": .string(jobID), "request_id": Self.requestID()])
        return response.job
    }

    // MARK: Destructive

    func discardTracks(meetingID: String, tracks: [String], scope: String?) async throws -> DiscardTracksResponse {
        var args: [String: JSONNode] = [
            "meeting_id": .string(meetingID),
            "tracks": .array(tracks.map(JSONNode.string)),
            "request_id": Self.requestID(),
        ]
        if let scope {
            args["scope"] = .string(scope)
        }
        return try await call(ToolCatalog.discardTracks, args)
    }

    /// Callers must have shown an explicit confirmation dialog before setting `confirm: true`.
    func deleteMeeting(meetingID: String, scope: String?) async throws -> DeleteMeetingResponse {
        var args: [String: JSONNode] = [
            "meeting_id": .string(meetingID),
            "confirm": .bool(true),
            "request_id": Self.requestID(),
        ]
        if let scope {
            args["scope"] = .string(scope)
        }
        return try await call(ToolCatalog.deleteMeeting, args)
    }

    // MARK: Import

    struct ImportRequest {
        var meetingName: String
        var micPath: String?
        var systemPath: String?
        var screenPath: String?
        var scope: String?
        var profile: String?
        var copy: Bool
        var autoProcess: Bool
    }

    func importRecording(_ request: ImportRequest) async throws -> ImportRecordingResponse {
        var args: [String: JSONNode] = [
            "meeting_name": .string(request.meetingName),
            "request_id": Self.requestID(),
        ]
        if let path = request.micPath, !path.isEmpty {
            args["mic_path"] = .string(path)
        }
        if let path = request.systemPath, !path.isEmpty {
            args["system_path"] = .string(path)
        }
        if let path = request.screenPath, !path.isEmpty {
            args["screen_path"] = .string(path)
        }
        if let scope = request.scope, !scope.isEmpty {
            args["scope"] = .string(scope)
        }
        if let profile = request.profile, !profile.isEmpty {
            args["profile"] = .string(profile)
        }
        if !request.copy {
            args["copy"] = .bool(false)
        }
        if !request.autoProcess {
            args["auto_process"] = .bool(false)
        }
        return try await call(ToolCatalog.importRecording, args)
    }

    // MARK: Profiles

    func profiles() async throws -> ListProfilesResponse {
        try await call(ToolCatalog.listProfiles)
    }

    func profile(name: String) async throws -> Profile {
        let response: ProfileResponse = try await call(ToolCatalog.getProfile, ["name": .string(name)])
        return response.profile
    }

    /// `updates` carries the set_profile keys to change (config / scope / engagement /
    /// export_destinations / make_default).
    func setProfile(name: String, updates: [String: JSONNode]) async throws -> Profile {
        var args = updates
        args["name"] = .string(name)
        args["request_id"] = Self.requestID()
        let response: ProfileResponse = try await call(ToolCatalog.setProfile, args)
        return response.profile
    }

    func deleteProfile(name: String) async throws -> DeleteProfileResponse {
        try await call(
            ToolCatalog.deleteProfile, ["name": .string(name), "request_id": Self.requestID()])
    }

    // MARK: Gaia connection (credentials are write-only)

    func gaiaConnection() async throws -> GaiaConnection {
        let response: GaiaConnectionResponse = try await call(ToolCatalog.getGaiaConnection)
        return response.connection
    }

    func setGaiaConnection(_ request: SetGaiaConnectionRequest) async throws -> GaiaConnection {
        let response: GaiaConnectionResponse = try await call(
            ToolCatalog.setGaiaConnection, Self.arguments(request))
        return response.connection
    }

    func testGaiaConnection(
        _ request: TestGaiaConnectionRequest = TestGaiaConnectionRequest()
    ) async throws -> GaiaConnectionTestResult {
        try await call(ToolCatalog.testGaiaConnection, Self.arguments(request))
    }

    // MARK: Diagnostics

    func serverInfo(
        refreshPermissions: Bool = false, contractVersion: String? = nil, serverInstanceID: String? = nil
    ) async throws -> ServerInfo {
        let arguments = RecordingPermissionContract.serverInfoArguments(
            contractVersion: contractVersion, serverInstanceID: serverInstanceID,
            refreshPermissions: refreshPermissions).mapValues(JSONNode.bool)
        return try await call(ToolCatalog.getServerInfo, arguments)
    }

    func configureRecordingPermission(
        _ permission: RecordingPermission, action: RecordingPermissionAction,
        requestID: String, contractVersion: String?, serverInstanceID: String?, sessionGeneration: UInt64
    ) async throws -> ConfigureRecordingPermissionResponse {
        guard RecordingPermissionContract.supportsSetup(contractVersion, serverInstanceID: serverInstanceID) else {
            throw ToolFailure(code: "unsupported", message: "権限設定には起動個体を確認できる対応サーバーが必要です。")
        }
        let response: ConfigureRecordingPermissionResponse = try await call(
            ToolCatalog.configureRecordingPermission,
            ["permission": .string(permission.rawValue), "action": .string(action.rawValue),
                "request_id": .string(requestID)], expectedSessionGeneration: sessionGeneration)
        guard response.permission == permission, response.action == action else {
            throw ToolFailure(code: "protocol", message: "権限設定の応答が要求と一致しません。状態を再確認してください。")
        }
        return response
    }

    func rebuildCatalog() async throws -> RebuildCatalogResponse {
        try await call(ToolCatalog.rebuildCatalog, ["request_id": Self.requestID()])
    }
}
