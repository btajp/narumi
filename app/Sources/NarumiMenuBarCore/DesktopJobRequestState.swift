import Foundation

/// Keeps job-producing requests unresolved until their outcome is confirmed. A lost
/// response is recovered with the original idempotency key and exact argument bytes.
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

    /// Call for a confirmed result or an authoritative rejection, never a transport error.
    @discardableResult
    public mutating func confirm(_ token: Token) -> Bool {
        guard let index = pending.firstIndex(where: { $0.inFlight == token }) else { return false }
        pending.remove(at: index)
        return true
    }

    /// Oldest unresolved request eligible for retry, in original registration order.
    /// Each request has at most one in-flight attempt; retry scheduling is external.
    public mutating func beginRetry() -> (request: Request, token: Token)? {
        guard let index = pending.firstIndex(where: { $0.inFlight == nil }) else { return nil }
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
}
