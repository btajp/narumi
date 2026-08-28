import Foundation
import Network
import Security

/// Real HTTPS fixture bound only to 127.0.0.1. The public test identity stays in memory;
/// no Keychain, DNS, subprocess, production credential or external service is used.
final class LoopbackTLSFixture: @unchecked Sendable {
    let port: UInt16
    var url: URL { URL(string: "https://127.0.0.1:\(port)/mcp")! }
    var certificateDER: Data { LoopbackTLSCertificate.certificateDER }
    var certificatePEM: String { LoopbackTLSCertificate.certificatePEM }
    var requests: [String] { state.requests }

    private let listener: NWListener
    private let state: ServerState

    init(status: Int = 200, location: String? = nil) throws {
        let tls = NWProtocolTLS.Options()
        sec_protocol_options_set_local_identity(tls.securityProtocolOptions, try Self.identity())
        sec_protocol_options_add_tls_application_protocol(tls.securityProtocolOptions, "http/1.1")
        sec_protocol_options_set_min_tls_protocol_version(tls.securityProtocolOptions, .TLSv12)
        let parameters = NWParameters(tls: tls)
        parameters.requiredLocalEndpoint = .hostPort(host: "127.0.0.1", port: .any)
        let listener = try NWListener(using: parameters, on: .any)
        let state = ServerState(status: status, location: location)
        self.listener = listener
        self.state = state
        listener.stateUpdateHandler = { [weak listener, state] update in
            state.observe(update, port: listener?.port?.rawValue)
        }
        listener.newConnectionHandler = { [state] connection in state.accept(connection) }
        listener.start(queue: state.queue)
        guard state.ready.wait(timeout: .now() + 5) == .success,
            let result = state.startResult
        else {
            state.stopConnections()
            listener.cancel()
            throw FixtureError.startTimeout
        }
        do {
            port = try result.get()
        } catch {
            state.stopConnections()
            listener.cancel()
            throw error
        }
    }

    deinit { stop() }

    func stop() {
        guard state.stopConnections() else { return }
        listener.cancel()
        _ = state.cancelled.wait(timeout: .now() + 5)
    }

    private static func identity() throws -> sec_identity_t {
        // This option is required even for test material: macOS otherwise imports
        // PKCS#12 identities into the user's Keychain. The app targets macOS 15+.
        let options: [String: Any] = [
            kSecImportExportPassphrase as String: LoopbackTLSCertificate.password,
            kSecImportToMemoryOnly as String: true,
        ]
        var imported: CFArray?
        let status = SecPKCS12Import(
            LoopbackTLSCertificate.identityPKCS12 as CFData, options as CFDictionary, &imported)
        guard status == errSecSuccess else { throw FixtureError.identityImport(status) }
        guard let items = imported as? [[String: Any]],
            let value = items.first?[kSecImportItemIdentity as String],
            CFGetTypeID(value as CFTypeRef) == SecIdentityGetTypeID(),
            let identity = sec_identity_create(value as! SecIdentity)
        else { throw FixtureError.identityMissing }
        return identity
    }

    private enum FixtureError: Error {
        case identityImport(OSStatus), identityMissing, listenerFailed, startTimeout
    }

    private final class ServerState: @unchecked Sendable {
        let queue = DispatchQueue(label: "narumi.test.loopback.tls")
        let ready = DispatchSemaphore(value: 0)
        let cancelled = DispatchSemaphore(value: 0)
        private let lock = NSLock()
        private let response: Data
        private let maxRequestBytes = 65_536
        private var initialResult: Result<UInt16, FixtureError>?
        private var stopped = false
        private var received: [String] = []
        private var connections: [ObjectIdentifier: NWConnection] = [:]

        var requests: [String] { lock.withLock { received } }
        var startResult: Result<UInt16, FixtureError>? { lock.withLock { initialResult } }

        init(status: Int, location: String?) {
            let redirect = location.map { "Location: \($0)\r\n" } ?? ""
            response = Data(
                "HTTP/1.1 \(status) Test\r\n\(redirect)Content-Length: 2\r\nConnection: close\r\n\r\n{}".utf8)
        }

        func observe(_ update: NWListener.State, port: UInt16?) {
            switch update {
            case .ready:
                finishStart(port.map { .success($0) } ?? .failure(.listenerFailed))
            case .failed:
                finishStart(.failure(.listenerFailed))
            case .cancelled:
                finishStart(.failure(.listenerFailed))
                cancelled.signal()
            default:
                break
            }
        }

        private func finishStart(_ result: Result<UInt16, FixtureError>) {
            let first = lock.withLock {
                guard initialResult == nil else { return false }
                initialResult = result
                return true
            }
            if first { ready.signal() }
        }

        @discardableResult
        func stopConnections() -> Bool {
            let active: [NWConnection]? = lock.withLock {
                guard !stopped else { return nil }
                stopped = true
                let active = Array(connections.values)
                connections.removeAll()
                return active
            }
            guard let active else { return false }
            for connection in active { connection.cancel() }
            return true
        }

        func accept(_ connection: NWConnection) {
            let accepted = lock.withLock {
                guard !stopped else { return false }
                connections[ObjectIdentifier(connection)] = connection
                return true
            }
            guard accepted else {
                connection.cancel()
                return
            }
            connection.stateUpdateHandler = { [weak self, weak connection] update in
                guard let self, let connection else { return }
                switch update {
                case .ready:
                    receive(connection, data: Data())
                case .failed, .cancelled:
                    remove(connection)
                default:
                    break
                }
            }
            connection.start(queue: queue)
        }

        private func remove(_ connection: NWConnection) {
            _ = lock.withLock { connections.removeValue(forKey: ObjectIdentifier(connection)) }
        }

        private func receive(_ connection: NWConnection, data: Data) {
            guard data.count < maxRequestBytes else {
                connection.cancel()
                return
            }
            connection.receive(minimumIncompleteLength: 1, maximumLength: maxRequestBytes - data.count) {
                [weak self, weak connection] content, _, complete, error in
                guard let self, let connection else { return }
                var request = data
                if let content { request.append(content) }
                if isComplete(request), let text = String(data: request, encoding: .utf8) {
                    let accepted = lock.withLock {
                        guard !stopped else { return false }
                        received.append(text)
                        return true
                    }
                    guard accepted else {
                        connection.cancel()
                        return
                    }
                    connection.send(content: response, completion: .contentProcessed { [weak connection] _ in
                        connection?.cancel()
                    })
                } else if complete || error != nil {
                    connection.cancel()
                } else {
                    receive(connection, data: request)
                }
            }
        }

        private func isComplete(_ data: Data) -> Bool {
            guard let range = data.range(of: Data("\r\n\r\n".utf8)),
                let header = String(data: data[..<range.lowerBound], encoding: .utf8)
            else { return false }
            let lengthHeader = header.components(separatedBy: "\r\n")
                .first { $0.lowercased().hasPrefix("content-length:") }
            let contentLength: Int
            if let lengthHeader {
                guard let length = Int(lengthHeader.dropFirst("content-length:".count)
                    .trimmingCharacters(in: .whitespaces)), length >= 0, length <= maxRequestBytes
                else { return false }
                contentLength = length
            } else {
                contentLength = 0
            }
            return data.count >= range.upperBound + contentLength
        }
    }
}
