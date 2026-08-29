import Foundation
import NarumiMenuBarCore

extension NarumiClient: TranscriptionRetryClient {
    func saveRetryEpoch(
        meetingID: String, scope: String?, expectedConfig: MeetingConfig,
        selection: TranscriptionModelSelection, requestID: String
    ) async throws -> SetMeetingConfigResponse {
        try await setMeetingConfig(
            meetingID: meetingID, scope: scope,
            updates: ["transcription_model": .object(try Self.arguments(selection))],
            expectedConfig: expectedConfig, requestID: requestID)
    }

    func regenerateRetry(
        meetingID: String, scope: String?, expectedConfig: MeetingConfig,
        retry: TranscriptionRetry, requestID: String
    ) async throws -> RegenerateResponse {
        try await regenerate(
            meetingID: meetingID, scope: scope, force: false, reason: "音声認識の結果不明区間を確認して再送",
            expectedConfig: expectedConfig, transcriptionRetry: retry, requestID: requestID)
    }
}

extension NarumiClient: TranscriptionRequestRecoveryClient {
    func pendingTranscriptionRequests() async -> [TranscriptionRequestRecovery] {
        await mcp.pendingTranscriptionRequests()
    }

    func recoverTranscriptionRequest(_ request: TranscriptionRequestRecovery) async throws -> RegenerateResponse {
        let result: ToolCallResult
        do {
            result = try await mcp.recoverTranscriptionRequest(request)
        } catch let error as MCPClientError {
            throw ToolFailure(from: error)
        }
        guard let structured = result.structuredContent else {
            throw ToolFailure(code: "protocol", message: "再送要求の受付結果を確認できません。自動では再送しません。")
        }
        do {
            return try JSONDecoder().decode(RegenerateResponse.self, from: structured.serialized())
        } catch {
            throw ToolFailure(code: "protocol", message: "再送要求の受付結果が不正です。ジョブの状態を確認してください。")
        }
    }
}
