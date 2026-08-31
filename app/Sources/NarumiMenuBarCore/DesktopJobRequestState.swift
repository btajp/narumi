import Foundation

/// Keeps job-producing requests unresolved until their outcome is confirmed. A lost
/// response is recovered with the original idempotency key and exact argument bytes,
/// except explicit audio retries and synchronous exports, which require manual recovery.
public struct DesktopJobRequestState: Equatable, Sendable {
    public struct Request: Equatable, Sendable {
        public let requestID: String
        public let tool: String
        public let arguments: Data

        public init(requestID: String, tool: String, arguments: Data) {
            self.requestID = requestID
            self.tool = tool
            self.arguments = arguments
        }

        public var canAutomaticallyRetry: Bool {
            if tool == ToolCatalog.prepareProviderRuntime { return false }
            if tool == ToolCatalog.regenerate {
                guard let values = try? JSONSerialization.jsonObject(with: arguments) as? [String: Any] else { return false }
                // A cancelled one-use audio confirmation must never be sent later by recovery.
                // Presence is enough to block replay, including malformed/null retry values.
                if values.keys.contains("transcription_retry") { return false }
            }
            guard tool == ToolCatalog.exportMinutes else { return true }
            // The server caches successes, not failures. A synchronous exporter may
            // have written remotely before failing; only queued exports are replayed.
            return (try? JSONDecoder().decode(ExportArguments.self, from: arguments))?.run_async == true
        }

        private struct ExportArguments: Decodable {
            let run_async: Bool?
        }
    }

    public struct Token: Equatable, Sendable {
        fileprivate let requestID: String
        fileprivate let revision: UInt64
    }

    private struct Pending: Equatable, Sendable {
        let request: Request
        var inFlight: Token?
        var isRetry: Bool
    }

    private var pending: [Pending] = []
    private var revision: UInt64 = 0

    public init() {}

    /// All unresolved requests, including initial sends and retries still in flight.
    public var pendingCount: Int { pending.count }
    public var uncertainCount: Int { pending.filter { $0.inFlight == nil }.count }
    public var pendingTools: Set<String> { Set(pending.map { $0.request.tool }) }

    public func requiresManualRecovery(requestID: String) -> Bool {
        pending.contains {
            $0.request.requestID == requestID && $0.inFlight == nil && !$0.request.canAutomaticallyRetry
        }
    }

    public var manualTranscriptionRequests: [Request] {
        pending.filter { $0.inFlight == nil && Self.isTranscriptionRetry($0.request) }.map(\.request)
    }

    public func hasTranscriptionRequest(requestID: String) -> Bool {
        pending.contains { $0.request.requestID == requestID && Self.isTranscriptionRetry($0.request) }
    }

    /// Only the explicit recovery screen may start this attempt, using the unchanged RAM record.
    public mutating func beginManualTranscriptionRecovery(_ request: Request) -> Token? {
        guard Self.isTranscriptionRetry(request), let index = pending.firstIndex(where: {
            $0.request == request && $0.inFlight == nil
        }) else { return nil }
        let token = nextToken(for: request.requestID)
        pending[index].inFlight = token
        pending[index].isRetry = true
        return token
    }

    public mutating func begin(_ request: Request) -> Token? {
        guard !pending.contains(where: { $0.request.requestID == request.requestID }) else { return nil }
        let token = nextToken(for: request.requestID)
        pending.append(Pending(request: request, inFlight: token, isRetry: false))
        return token
    }

    /// A transport/query failure must not discard a request that may have taken effect.
    @discardableResult
    public mutating func markUncertain(_ token: Token) -> Bool {
        guard let index = pending.firstIndex(where: { $0.inFlight == token }) else { return false }
        pending[index].inFlight = nil
        pending[index].isRetry = false
        return true
    }

    /// Only the first attempt can prove a preflight rejection. A replay error cannot
    /// rule out work accepted before the original response was lost.
    @discardableResult
    public mutating func finishFailure(_ token: Token, errorCode: String?) -> Bool {
        guard let index = pending.firstIndex(where: { $0.inFlight == token }) else { return false }
        let attempt = pending[index]
        if !attempt.isRetry, Self.isPreflightRejection(errorCode, tool: attempt.request.tool) {
            pending.remove(at: index)
            return true
        }
        return markUncertain(token)
    }

    /// Call for a confirmed result or an authoritative rejection, never a transport error.
    @discardableResult
    public mutating func confirm(_ token: Token) -> Bool {
        guard let index = pending.firstIndex(where: { $0.inFlight == token }) else { return false }
        pending.remove(at: index)
        return true
    }

    /// A read-only provider lookup can recover an accepted setup without rerunning it.
    @discardableResult
    public mutating func confirmProviderSetup(requestID: String, providerID: String, resourceID: String) -> Bool {
        guard let index = pending.firstIndex(where: {
            guard $0.request.requestID == requestID, $0.request.tool == ToolCatalog.prepareProviderRuntime,
                let arguments = try? JSONSerialization.jsonObject(with: $0.request.arguments) as? [String: Any]
            else { return false }
            return arguments["provider_id"] as? String == providerID && arguments["resource_id"] as? String == resourceID
        }) else { return false }
        pending.remove(at: index)
        return true
    }

    /// Oldest unresolved request eligible for retry, in original registration order.
    /// Each request has at most one in-flight attempt; retry scheduling is external.
    public mutating func beginRetry() -> (request: Request, token: Token)? {
        guard let index = pending.firstIndex(where: {
            $0.inFlight == nil && $0.request.canAutomaticallyRetry
        }) else { return nil }
        let request = pending[index].request
        let token = nextToken(for: request.requestID)
        pending[index].inFlight = token
        pending[index].isRetry = true
        return (request, token)
    }

    /// Connection changes invalidate retry callbacks without abandoning their requests.
    /// Original sends keep their tokens so their own completion can still resolve them.
    public mutating func invalidateRetries() {
        for index in pending.indices where pending[index].isRetry {
            pending[index].inFlight = nil
            pending[index].isRetry = false
        }
    }

    private mutating func nextToken(for requestID: String) -> Token {
        revision &+= 1
        return Token(requestID: requestID, revision: revision)
    }

    private static func isTranscriptionRetry(_ request: Request) -> Bool {
        guard request.tool == ToolCatalog.regenerate,
            let values = try? JSONSerialization.jsonObject(with: request.arguments) as? [String: Any] else { return false }
        return values.keys.contains("transcription_retry")
    }

    private static func isPreflightRejection(_ code: String?, tool: String) -> Bool {
        guard let code else { return false }
        switch tool {
        case ToolCatalog.prepareProviderRuntime:
            return ["invalid_argument", "not_found", "configuration_conflict", "busy",
                "engine_unavailable", "authentication_required"].contains(code)
        case ToolCatalog.regenerate:
            return ["invalid_argument", "not_found", "busy", "scope_denied", "policy_violation",
                "engine_unavailable", "configuration_conflict", "authentication_required", "model_unavailable"].contains(code)
        case ToolCatalog.importRecording:
            return ["invalid_argument", "not_found", "busy", "policy_violation", "engine_unavailable"].contains(code)
        case ToolCatalog.registerContext, ToolCatalog.exportMinutes:
            // Export engine failures can follow a successful remote write, unlike the
            // explicit configuration preflight in regenerate/import_recording.
            return ["invalid_argument", "not_found", "busy", "scope_denied"].contains(code)
        case ToolCatalog.stopRecording:
            return ["invalid_argument", "not_found", "busy"].contains(code)
        default:
            return false
        }
    }
}
