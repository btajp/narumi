enum MCPClientSessionFixtureSourceASRRecovery {
    static let text = #"""

    private final class FixtureJobNotifications: @unchecked Sendable {
        private let lock = NSLock()
        private var snapshots: [(Int, Set<String>)] = []

        func record(pending: Int, jobs: Set<String>) { lock.withLock { snapshots.append((pending, jobs)) } }
        func recorded(jobID: String) -> Bool { lock.withLock { snapshots.contains { $0.0 == 0 && $0.1.contains(jobID) } } }
    }

    private extension Fixture {
        static func checkTranscriptionRetryLoss(
            client: NarumiClient, wire: FakeHTTP, accepted: Bool
        ) async throws -> [String: Bool] {
            let confirmation = try transcriptionRetryConfirmation()
            let confirmationNode = JSONNode.object(try NarumiClient.arguments(confirmation))
            let requestID = UUID().uuidString
            var manualMessage = false
            var checks: [String: Bool] = [:]
            do {
                _ = try await client.regenerate(
                    meetingID: "20260829T000000Z-a1b2c3d4", scope: nil, force: false, reason: nil,
                    expectedConfig: transcriptionConfig(modelID: "whisper-1", epoch: 4),
                    transcriptionRetry: confirmation, requestID: requestID)
            } catch let failure as ToolFailure {
                manualMessage = failure.message.contains("音声") && failure.message.contains("自動")
                    && !failure.message.contains("エクスポート") && !failure.message.contains(fixtureSecret)
                    && failure.transcriptionOutcome == nil
            } catch {}
            let before = await client.mcp.jobRequests
            for _ in 0..<20 { await client.mcp.recoverPendingJobCalls() }
            let duplicates: [(String, String?, TranscriptionRetry?)] = [
                ("duplicate_same_body_blocked", nil, confirmation), ("duplicate_changed_body_blocked", "changed body", nil),
            ]
            for (name, reason, retry) in duplicates {
                var blocked = false
                do {
                    _ = try await client.regenerate(
                        meetingID: "20260829T000000Z-a1b2c3d4", scope: nil, force: false, reason: reason,
                        expectedConfig: transcriptionConfig(modelID: "whisper-1", epoch: 4),
                        transcriptionRetry: retry, requestID: requestID)
                } catch { blocked = true }
                let ledger = await client.mcp.jobRequests
                checks[name] = blocked && wire.count(ToolCatalog.regenerate) == 1 && ledger.pendingCount == 1
            }
            let after = await client.mcp.jobRequests
            let posts = wire.arguments(ToolCatalog.regenerate)
            checks.merge([
                "sent_once": posts.count == 1 && posts.first?["transcription_retry"] == confirmationNode
                    && posts.first?["request_id"] == .string(requestID)
                    && wire.httpMethods(ToolCatalog.regenerate) == ["POST"]
                    && wire.acceptedGenerationCount == (accepted ? 1 : 0),
                "pending_retained": before.pendingCount == 1 && before.uncertainCount == 1
                    && after.pendingCount == 1 && after.uncertainCount == 1
                    && after.pendingTools == [ToolCatalog.regenerate] && after.requiresManualRecovery(requestID: requestID),
                "manual_message": manualMessage,
            ]) { _, new in new }
            return checks
        }

        static func checkTranscriptionRetryRejection(
            client: NarumiClient, wire: FakeHTTP, code: String
        ) async throws -> Bool {
            let requestID = UUID().uuidString
            var refused = false
            do {
                _ = try await client.regenerate(
                    meetingID: "20260829T000000Z-a1b2c3d4", scope: nil, force: false, reason: nil,
                    expectedConfig: transcriptionConfig(modelID: "whisper-1", epoch: 4),
                    transcriptionRetry: transcriptionRetryConfirmation(), requestID: requestID)
            } catch let failure as ToolFailure { refused = failure.code == code }
            let before = await client.mcp.jobRequests
            for _ in 0..<20 { await client.mcp.recoverPendingJobCalls() }
            let after = await client.mcp.jobRequests
            let posts = wire.arguments(ToolCatalog.regenerate)
            return refused && before.pendingCount == 0 && after.pendingCount == 0 && after.uncertainCount == 0
                && posts.count == 1 && posts.first?["transcription_retry"] != nil
                && wire.httpMethods(ToolCatalog.regenerate) == ["POST"]
                && posts.first?["request_id"] == .string(requestID) && wire.acceptedGenerationCount == 0
        }

        static func checkOrdinaryGenerationRecovery(
            client: NarumiClient, wire: FakeHTTP, minutes: Bool
        ) async throws -> Bool {
            var config = try transcriptionConfig(modelID: "whisper-1", epoch: 4)
            if minutes {
                config.transcriptionModel = nil
                config.minutesModel = MinutesModelSelection(
                    provider: "codex-app-server", connectionID: "conn-0123456789ab", connectionRevision: 4,
                    modelID: "fixture-codex-model", cacheEpoch: 3)
            }
            let requestID = UUID().uuidString
            var lost = false
            do {
                _ = try await client.regenerate(
                    meetingID: "20260829T000000Z-a1b2c3d4", scope: nil, force: false, reason: nil,
                    expectedConfig: config, requestID: requestID)
            } catch { lost = true }
            let before = await client.mcp.jobRequests
            for _ in 0..<20 { await client.mcp.recoverPendingJobCalls() }
            let after = await client.mcp.jobRequests
            let posts = wire.arguments(ToolCatalog.regenerate)
            return lost && before.pendingCount == 1 && after.pendingCount == 0 && wire.acceptedGenerationCount == 1
                && posts.count == 2 && posts[0] == posts[1] && posts[0]["transcription_retry"] == nil
                && wire.httpMethods(ToolCatalog.regenerate) == ["POST", "POST"]
                && posts[0]["request_id"] == .string(requestID)
        }

        static func checkManualTranscriptionRecovery(
            client: NarumiClient, wire: FakeHTTP, notifications: FixtureJobNotifications, outcome: String
        ) async throws -> [String: Bool] {
            var checks = Dictionary(uniqueKeysWithValues: [
                "pending_read_local", "wrong_body_rejected", "one_manual_send", "result_state", "no_automatic_followup",
            ].map { ($0, false) })
            if outcome == "success" { checks["stale_record_rejected"] = false }
            let config = try transcriptionConfig(modelID: "whisper-1", epoch: 4)
            let confirmation = try transcriptionRetryConfirmation()
            let requestID = UUID().uuidString
            _ = try? await client.regenerate(
                meetingID: "20260829T000000Z-a1b2c3d4", scope: "fixture-scope", force: false, reason: nil,
                expectedConfig: config, transcriptionRetry: confirmation, requestID: requestID)
            let callsBeforeRead = wire.calls
            let pending = await client.pendingTranscriptionRequests()
            guard let original = pending.first else { return checks }
            checks["pending_read_local"] = pending.count == 1 && wire.calls == callsBeforeRead
                && original.requestID == requestID && original.meetingID == "20260829T000000Z-a1b2c3d4"
                && original.scope == "fixture-scope" && original.expectedConfig == config
                && original.transcriptionRetry == confirmation
            guard case .object(var wrongArguments) = try JSONNode.parse(original.arguments) else { return checks }
            wrongArguments["reason"] = .string("unconfirmed change to the original request")
            let changed = try TranscriptionRequestRecovery(request: .init(
                requestID: original.requestID, tool: ToolCatalog.regenerate,
                arguments: JSONNode.object(wrongArguments).serialized()))
            var changedRejected = false
            do { _ = try await client.recoverTranscriptionRequest(changed) }
            catch { changedRejected = true }
            let afterChanged = await client.pendingTranscriptionRequests()
            checks["wrong_body_rejected"] = changedRejected && wire.count(ToolCatalog.regenerate) == 1
                && afterChanged == pending

            var receipt: RegenerateResponse?
            var manualFailure = false
            do { receipt = try await client.recoverTranscriptionRequest(original) }
            catch let failure as ToolFailure {
                manualFailure = failure.message.contains("音声") && !failure.message.contains("エクスポート")
                    && !failure.message.contains(fixtureSecret) && failure.transcriptionOutcome == nil
            } catch {}
            let posts = wire.arguments(ToolCatalog.regenerate)
            checks["one_manual_send"] = posts.count == 2 && posts[0] == posts[1]
                && posts[0]["request_id"] == .string(requestID)
                && wire.httpMethods(ToolCatalog.regenerate) == ["POST", "POST"]
                && wire.acceptedGenerationCount == 1
            let ledger = await client.mcp.jobRequests
            let remaining = await client.pendingTranscriptionRequests()
            if outcome == "success" {
                checks["result_state"] = receipt?.jobID == "job-0123456789ab"
                    && receipt?.meetingID == original.meetingID && ledger.pendingCount == 0 && remaining.isEmpty
                    && notifications.recorded(jobID: "job-0123456789ab")
                var staleRejected = false
                do { _ = try await client.recoverTranscriptionRequest(original) }
                catch { staleRejected = true }
                checks["stale_record_rejected"] = staleRejected && wire.count(ToolCatalog.regenerate) == 2
            } else {
                checks["result_state"] = receipt == nil && manualFailure && ledger.pendingCount == 1
                    && ledger.uncertainCount == 1 && remaining == pending
                    && !notifications.recorded(jobID: "job-0123456789ab")
            }
            for _ in 0..<20 { await client.mcp.recoverPendingJobCalls() }
            let finalLedger = await client.mcp.jobRequests
            checks["no_automatic_followup"] = wire.count(ToolCatalog.regenerate) == 2
                && finalLedger.pendingCount == (outcome == "success" ? 0 : 1)
            return checks
        }

        static func transcriptionRetryConfirmation() throws -> TranscriptionRetry {
            try TranscriptionRetry(
                inputFingerprint: String(repeating: "a", count: 64),
                chunkFingerprint: String(repeating: "b", count: 64), blockedEpoch: 3)
        }

    }

    """#
}
