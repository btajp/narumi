import Darwin
import XCTest

@testable import NarumiMenuBarCore

final class RuntimeGuardsTests: XCTestCase {
    func testBusyPortIsRejectedWithoutClosingExistingListener() throws {
        let listener = try Listener()
        XCTAssertThrowsError(try LocalServerPort.requireAvailable(listener.port)) { error in
            XCTAssertEqual((error as? LocalServerPort.Unavailable)?.code, EADDRINUSE)
            XCTAssertEqual((error as? LocalServerPort.Unavailable)?.port, listener.port)
        }
        try withExtendedLifetime(listener) {
            XCTAssertGreaterThanOrEqual(fcntl(listener.descriptor, F_GETFD), 0, "the unowned descriptor stays open")
            XCTAssertThrowsError(try LocalServerPort.requireAvailable(listener.port), "the listener still owns its port")
        }
    }

    func testReleasedPortIsAvailable() throws {
        var listener: Listener? = try Listener()
        let port = try XCTUnwrap(listener?.port)
        listener = nil
        XCTAssertNoThrow(try LocalServerPort.requireAvailable(port))
    }

    func testInvalidPortsFailClosed() {
        for port in [0, -1, 65536, Int.max] {
            XCTAssertThrowsError(try LocalServerPort.requireAvailable(port))
        }
    }

    func testRuntimeLeasePreventsConcurrentSyncAndReleasesOnExit() throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent("narumi-lease-\(UUID().uuidString)")
        defer { try? FileManager.default.removeItem(at: root) }
        let paths = RuntimePaths(dataRoot: root)
        var first: RuntimeLease? = try RuntimeLease(paths: paths)
        XCTAssertNotNil(first)
        XCTAssertThrowsError(try RuntimeLease(paths: paths))
        first = nil
        let next = try RuntimeLease(paths: paths)
        try withExtendedLifetime(next) {
            XCTAssertThrowsError(try RuntimeLease(paths: paths))
        }
    }

    private final class Listener {
        let descriptor: Int32
        let port: Int

        init() throws {
            let fd = socket(AF_INET, SOCK_STREAM, 0)
            guard fd >= 0 else { throw POSIXError(.EIO) }
            var address = sockaddr_in()
            address.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
            address.sin_family = sa_family_t(AF_INET)
            address.sin_port = 0
            address.sin_addr = in_addr(s_addr: inet_addr("127.0.0.1"))
            var length = socklen_t(MemoryLayout<sockaddr_in>.size)
            let bound = withUnsafeMutablePointer(to: &address) { pointer in
                pointer.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                    Darwin.bind(fd, $0, length) == 0 && listen(fd, 1) == 0
                        && getsockname(fd, $0, &length) == 0
                }
            }
            guard bound else {
                close(fd)
                throw POSIXError(.EIO)
            }
            descriptor = fd
            port = Int(UInt16(bigEndian: address.sin_port))
        }

        deinit { close(descriptor) }
    }
}
