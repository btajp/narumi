import Darwin
import Foundation

/// Bounded pipe I/O prevents a stalled helper or oversized reply from hanging the app.
/// No Keychain value is placed in argv, the environment, a file, or stderr.
struct KeychainHelperProcessExecutor: KeychainHelperExecuting {
    private let timeoutNanoseconds: UInt64

    init(timeoutNanoseconds: UInt64 = 5_000_000_000) {
        self.timeoutNanoseconds = timeoutNanoseconds
    }

    func run(executable: URL, input: Data) throws -> KeychainHelperResult {
        // The reader sends only the small get request; a generic mutation launcher
        // must not reuse this synchronous input path with arbitrary payloads.
        guard executable.isFileURL, input.count <= 1024 else { throw unavailable }
        let process = Process()
        let stdin = Pipe()
        let stdout = Pipe()
        process.executableURL = executable
        process.arguments = []
        process.environment = [:]
        process.standardInput = stdin
        process.standardOutput = stdout
        process.standardError = FileHandle.nullDevice
        defer {
            try? stdin.fileHandleForReading.close()
            try? stdin.fileHandleForWriting.close()
            try? stdout.fileHandleForReading.close()
            try? stdout.fileHandleForWriting.close()
            stopIfRunning(process)
        }
        do {
            try process.run()
            try stdin.fileHandleForReading.close()
            try stdout.fileHandleForWriting.close()
            guard fcntl(stdin.fileHandleForWriting.fileDescriptor, F_SETNOSIGPIPE, 1) == 0 else {
                throw unavailable
            }
            try stdin.fileHandleForWriting.write(contentsOf: input)
            try stdin.fileHandleForWriting.close()
            let output = try receive(process: process, handle: stdout.fileHandleForReading)
            process.waitUntilExit()
            guard process.terminationReason == .exit else { throw unavailable }
            return KeychainHelperResult(output: output, exitStatus: process.terminationStatus)
        } catch {
            throw unavailable
        }
    }

    private func receive(process: Process, handle: FileHandle) throws -> Data {
        let descriptor = handle.fileDescriptor
        let flags = fcntl(descriptor, F_GETFL)
        guard flags >= 0, fcntl(descriptor, F_SETFL, flags | O_NONBLOCK) == 0 else {
            throw unavailable
        }
        let start = DispatchTime.now().uptimeNanoseconds
        var output = Data()
        var buffer = [UInt8](repeating: 0, count: 4096)
        var eof = false
        while !eof || process.isRunning {
            guard DispatchTime.now().uptimeNanoseconds - start < timeoutNanoseconds else {
                throw unavailable
            }
            var event = pollfd(fd: descriptor, events: Int16(POLLIN | POLLHUP), revents: 0)
            let ready = poll(&event, 1, 25)
            if ready < 0 {
                if errno == EINTR { continue }
                throw unavailable
            }
            if event.revents & Int16(POLLNVAL | POLLERR) != 0 { throw unavailable }
            if !eof && event.revents & Int16(POLLIN | POLLHUP) != 0 {
                let count = Darwin.read(descriptor, &buffer, buffer.count)
                if count > 0 {
                    output.append(contentsOf: buffer.prefix(count))
                    guard output.count <= KeychainHelperProtocol.maximumResponseBytes else {
                        throw unavailable
                    }
                } else if count == 0 {
                    eof = true
                } else if errno != EAGAIN && errno != EINTR {
                    throw unavailable
                }
            }
            if eof && process.isRunning {
                // A helper that closes stdout but keeps running still shares the deadline.
                var empty = pollfd(fd: -1, events: 0, revents: 0)
                _ = poll(&empty, 1, 25)
            }
        }
        return output
    }

    private func stopIfRunning(_ process: Process) {
        guard process.isRunning else { return }
        // This is our own short-lived child, not a shared server or user process.
        _ = kill(process.processIdentifier, SIGKILL)
        process.waitUntilExit()
    }

    private var unavailable: KeychainSecretError { .keychainUnavailable }
}
