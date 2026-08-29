import NarumiMenuBarCore

extension NarumiClient: ProcessingRunHistoryClient {
    // `connectionGeneration` belongs to the MainWindow store's UI context. The MCP actor has
    // an independent session generation, captured immediately before each local read below.
    func listProcessingRuns(
        meetingID: String, scope: String?, limit: Int, cursor: String?,
        connectionGeneration _: UInt64
    ) async throws -> ListProcessingRunsResponse {
        var arguments: [String: JSONNode] = [
            "meeting_id": .string(meetingID),
            "limit": .number(Double(limit)),
        ]
        if let scope { arguments["scope"] = .string(scope) }
        if let cursor { arguments["cursor"] = .string(cursor) }
        let sessionGeneration = await mcp.operationSessionGeneration
        let response: ListProcessingRunsResponse = try await call(
            ToolCatalog.listProcessingRuns, arguments,
            expectedSessionGeneration: sessionGeneration)
        guard response.meetingID == meetingID else {
            throw ToolFailure(code: "protocol", message: "生成履歴の会議が要求と一致しません。")
        }
        return response
    }

    func getProcessingRun(
        meetingID: String, scope: String?, runID: String,
        connectionGeneration _: UInt64
    ) async throws -> GetProcessingRunResponse {
        var arguments: [String: JSONNode] = [
            "meeting_id": .string(meetingID),
            "run_id": .string(runID),
        ]
        if let scope { arguments["scope"] = .string(scope) }
        let sessionGeneration = await mcp.operationSessionGeneration
        let response: GetProcessingRunResponse = try await call(
            ToolCatalog.getProcessingRun, arguments,
            expectedSessionGeneration: sessionGeneration)
        guard response.meetingID == meetingID, response.run.runID == runID else {
            throw ToolFailure(code: "protocol", message: "生成runが要求した会議・runと一致しません。")
        }
        return response
    }

    func getProcessingArtifact(
        meetingID: String, scope: String?, runID: String, artifactID: String,
        connectionGeneration _: UInt64
    ) async throws -> ProcessingArtifactResponse {
        var arguments: [String: JSONNode] = [
            "meeting_id": .string(meetingID),
            "run_id": .string(runID),
            "artifact_id": .string(artifactID),
        ]
        if let scope { arguments["scope"] = .string(scope) }
        let sessionGeneration = await mcp.operationSessionGeneration
        let response: ProcessingArtifactResponse = try await call(
            ToolCatalog.getProcessingArtifact, arguments,
            expectedSessionGeneration: sessionGeneration)
        guard response.requestedRunID == runID,
            response.artifact.artifactID == artifactID else {
            throw ToolFailure(code: "protocol", message: "生成成果が要求したrun・成果IDと一致しません。")
        }
        return response
    }
}
