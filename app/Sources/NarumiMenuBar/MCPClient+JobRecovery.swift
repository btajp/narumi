import Foundation
import NarumiMenuBarCore

extension MCPClient {
    /// Only job-producing tools participate. Their original arguments remain in RAM until
    /// the server confirms the outcome; credentials and other tools never enter this ledger.
    func performJobTrackedToolCall(
        _ name: String, arguments: [String: JSONNode], confidential: Bool
    ) async throws -> ToolCallResult {
        guard Self.createsJob(name, arguments),
            let requestID = arguments["request_id"]?.stringValue
        else {
            return try await performToolCall(name, arguments: arguments, confidential: confidential)
        }
        let request = DesktopJobRequestState.Request(
            requestID: requestID, tool: name, arguments: try JSONNode.object(arguments).serialized())
        guard let token = jobRequests.begin(request) else {
            return try await performToolCall(name, arguments: arguments, confidential: confidential)
        }
        await publishJobRequestState()
        do {
            let result = try await performToolCall(name, arguments: arguments, confidential: confidential)
            await finishJobRequest(token, tool: name, result: result)
            if jobRequests.requiresManualRecovery(requestID: requestID) {
                throw MCPClientError.tool(message: Self.uncertainMessage(tool: name), payload: nil)
            }
            return result
        } catch {
            jobRequests.finishFailure(token, errorCode: Self.toolErrorCode(error))
            await publishJobRequestState()
            if jobRequests.requiresManualRecovery(requestID: requestID) {
                throw MCPClientError.tool(message: Self.uncertainMessage(tool: name), payload: nil)
            }
            throw error
        }
    }

    /// The public provider catalog preserves setup request/job IDs even after a response is
    /// lost. Reconcile those IDs locally; never resend an installer or replace its request ID.
    func reconcileProviderSetupRequests(_ result: ToolCallResult) async {
        guard case .array(let providers)? = result.structuredContent?["providers"] else { return }
        for provider in providers {
            guard let providerID = provider["provider_id"]?.stringValue else { continue }
            for field in ["active_setup", "last_setup"] {
                guard let operation = provider["runtime"]?[field],
                    let requestID = operation["start_request_id"]?.stringValue,
                    let resourceID = operation["resource_id"]?.stringValue,
                    let jobID = operation["job_id"]?.stringValue,
                    jobID.range(of: "^job-[0-9a-f]{12,32}$", options: .regularExpression) != nil,
                    jobRequests.confirmProviderSetup(requestID: requestID, providerID: providerID, resourceID: resourceID)
                else { continue }
                await publishJobRequestState(jobID: jobID)
            }
        }
    }

    /// One retry per status tick avoids a retry loop. A transport, protocol or internal
    /// failure never clears an unknown outcome, including errors from the replay itself.
    func recoverPendingJobCalls() async {
        if jobRequests.pendingTools.contains(ToolCatalog.prepareProviderRuntime) {
            // A local metadata lookup is safe even when the provider sheet has closed.
            // Its response reconciles the original request in performToolCall.
            _ = try? await performToolCall(ToolCatalog.listProviders, arguments: [:], confidential: false)
        }
        guard let retry = jobRequests.beginRetry() else { return }
        do {
            guard case .object(let arguments) = try JSONNode.parse(retry.request.arguments) else {
                jobRequests.markUncertain(retry.token)
                return
            }
            let result = try await performToolCall(retry.request.tool, arguments: arguments, confidential: false)
            await finishJobRequest(retry.token, tool: retry.request.tool, result: result)
        } catch {
            jobRequests.finishFailure(retry.token, errorCode: Self.toolErrorCode(error))
            await publishJobRequestState()
        }
    }

    private func finishJobRequest(
        _ token: DesktopJobRequestState.Token, tool: String, result: ToolCallResult
    ) async {
        let jobID = result.structuredContent?["job_id"]?.stringValue
        let synchronousExport = tool == ToolCatalog.exportMinutes
            && result.structuredContent?["result"]?["ref"]?.stringValue != nil
        if (jobID?.isEmpty == false || synchronousExport), jobRequests.confirm(token) {
            // The main actor tracks the job before clearing the unknown-request block,
            // so there is no update-allowed gap between response and typed decoding.
            await publishJobRequestState(jobID: jobID)
        } else {
            jobRequests.markUncertain(token)
            await publishJobRequestState()
        }
    }

    private func publishJobRequestState(jobID: String? = nil) async {
        jobRequestPublication &+= 1
        let publication = jobRequestPublication
        if let jobID { unpublishedJobIDs[jobID] = publication }
        await jobRequestObserver?(
            publication, jobRequests.pendingCount,
            jobRequests.pendingTools.contains(ToolCatalog.stopRecording), Set(unpublishedJobIDs.keys))
        // Reentrant calls may have discovered more jobs while the observer was running.
        // Retain those for their own acknowledgement/newer snapshot.
        unpublishedJobIDs = unpublishedJobIDs.filter { $0.value > publication }
    }

    private static func createsJob(_ name: String, _ arguments: [String: JSONNode]) -> Bool {
        MCPToolReplayPolicy.createsJob(
            tool: name, autoProcess: arguments["auto_process"]?.boolValue,
            autoRegenerate: arguments["auto_regenerate"]?.boolValue)
    }

    private static func toolErrorCode(_ error: any Error) -> String? {
        guard case MCPClientError.tool(_, let payload) = error else { return nil }
        return payload?["error"]?["code"]?.stringValue
    }

    private static func uncertainMessage(tool: String) -> String {
        if tool == ToolCatalog.prepareProviderRuntime {
            return "プロバイダ準備の受付結果を確認できません。自動再送せず、接続設定画面から元のジョブを再確認します。"
        }
        return "エクスポート結果を確認できません。重複送信を避けるため自動再送はせず、録画開始と更新を保留しています。送信先の状態を確認してください。"
    }
}
