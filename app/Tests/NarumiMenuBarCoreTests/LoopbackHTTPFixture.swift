import Darwin
import Foundation

/// Real loopback-only HTTP fixture: no DNS, subprocesses, production data or external services.
final class LoopbackHTTPFixture: @unchecked Sendable {
    let port: UInt16
    var url: URL { URL(string: "http://127.0.0.1:\(port)/mcp")! }

    private let descriptor: Int32
    private let lock = NSLock()
    private let completed = DispatchSemaphore(value: 0)
    private var stopped = false
    private var received: [String] = []
    private let response: Data

    var requests: [String] { lock.withLock { received } }

    init(status: Int = 200, location: String? = nil) throws {
        let socketDescriptor = socket(AF_INET, SOCK_STREAM, 0)
        guard socketDescriptor >= 0 else { throw FixtureError.socket }
        descriptor = socketDescriptor
        var address = sockaddr_in()
        address.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
        address.sin_family = sa_family_t(AF_INET)
        address.sin_addr.s_addr = inet_addr("127.0.0.1")
        let bound = withUnsafePointer(to: &address) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                Darwin.bind(socketDescriptor, $0, socklen_t(MemoryLayout<sockaddr_in>.size))
            }
        }
        guard bound == 0, listen(descriptor, 4) == 0 else {
            close(descriptor)
            throw FixtureError.socket
        }
        var length = socklen_t(MemoryLayout<sockaddr_in>.size)
        let named = withUnsafeMutablePointer(to: &address) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { getsockname(socketDescriptor, $0, &length) }
        }
        guard named == 0 else {
            close(descriptor)
            throw FixtureError.socket
        }
        port = UInt16(bigEndian: address.sin_port)
        let redirect = location.map { "Location: \($0)\r\n" } ?? ""
        response = Data("HTTP/1.1 \(status) Test\r\n\(redirect)Content-Length: 2\r\nConnection: close\r\n\r\n{}".utf8)
        DispatchQueue(label: "narumi.test.loopback.\(port)").async { [self] in serve() }
    }

    func stop() {
        let first = lock.withLock {
            guard !stopped else { return false }
            stopped = true
            return true
        }
        if first { _ = completed.wait(timeout: .now() + 5) }
    }

    private func serve() {
        defer {
            close(descriptor)
            completed.signal()
        }
        while !lock.withLock({ stopped }) {
            var pending = pollfd(fd: descriptor, events: Int16(POLLIN), revents: 0)
            guard poll(&pending, 1, 100) > 0 else { continue }
            let client = accept(descriptor, nil, nil)
            guard client >= 0 else { continue }
            handle(client)
            close(client)
        }
    }

    private func handle(_ client: Int32) {
        var timeout = timeval(tv_sec: 2, tv_usec: 0)
        _ = setsockopt(client, SOL_SOCKET, SO_RCVTIMEO, &timeout, socklen_t(MemoryLayout<timeval>.size))
        var noSignal: Int32 = 1
        _ = setsockopt(client, SOL_SOCKET, SO_NOSIGPIPE, &noSignal, socklen_t(MemoryLayout<Int32>.size))
        var request = Data()
        var buffer = [UInt8](repeating: 0, count: 4096)
        while request.count < 65_536 {
            let count = recv(client, &buffer, buffer.count, 0)
            guard count > 0 else { return }
            request.append(contentsOf: buffer.prefix(count))
            if isComplete(request) { break }
        }
        guard isComplete(request), let text = String(data: request, encoding: .utf8) else { return }
        lock.withLock { received.append(text) }
        response.withUnsafeBytes { raw in
            guard let base = raw.baseAddress else { return }
            var written = 0
            while written < raw.count {
                let count = send(client, base.advanced(by: written), raw.count - written, 0)
                guard count > 0 else { return }
                written += count
            }
        }
    }

    private func isComplete(_ data: Data) -> Bool {
        guard let range = data.range(of: Data("\r\n\r\n".utf8)),
            let header = String(data: data[..<range.lowerBound], encoding: .utf8)
        else { return false }
        let contentLength = header.components(separatedBy: "\r\n")
            .first { $0.lowercased().hasPrefix("content-length:") }
            .flatMap { Int($0.dropFirst("content-length:".count).trimmingCharacters(in: .whitespaces)) } ?? 0
        return data.count >= range.upperBound + contentLength
    }

    private enum FixtureError: Error { case socket }
}
