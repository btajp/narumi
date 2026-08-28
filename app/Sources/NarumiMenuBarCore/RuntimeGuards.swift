import Darwin
import Foundation

/// The bind probe never talks to, adopts or signals the process using a port. It also
/// catches a non-MCP listener, which a get_server_info-only check would overlook.
public enum LocalServerPort {
    public struct Unavailable: LocalizedError {
        public let port: Int
        public let code: Int32
        public var errorDescription: String? {
            if code == EADDRINUSE {
                return "ポート \(port) は別のサーバーが使用中です。既存サーバーを終了してから再試行してください（自動停止はしません）"
            }
            return "ポート \(port) の空き状態を確認できません（errno \(code)）"
        }
    }

    public static func requireAvailable(_ port: Int) throws {
        guard (1...65535).contains(port) else { throw Unavailable(port: port, code: EINVAL) }
        let descriptor = socket(AF_INET, SOCK_STREAM, 0)
        guard descriptor >= 0 else { throw Unavailable(port: port, code: errno) }
        defer { close(descriptor) }
        // Match uvicorn's SO_REUSEADDR so connections in TIME_WAIT after a graceful stop
        // do not look like a live listener. SO_REUSEPORT is deliberately never enabled.
        var reuse: Int32 = 1
        guard setsockopt(descriptor, SOL_SOCKET, SO_REUSEADDR, &reuse, socklen_t(MemoryLayout.size(ofValue: reuse))) == 0 else {
            throw Unavailable(port: port, code: errno)
        }
        var address = sockaddr_in()
        address.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
        address.sin_family = sa_family_t(AF_INET)
        address.sin_port = UInt16(port).bigEndian
        address.sin_addr = in_addr(s_addr: inet_addr("127.0.0.1"))
        let result = withUnsafePointer(to: &address) { pointer in
            pointer.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                Darwin.bind(descriptor, $0, socklen_t(MemoryLayout<sockaddr_in>.size))
            }
        }
        guard result == 0 else { throw Unavailable(port: port, code: errno) }
    }
}

/// Two app instances must not prepare or use the same venv concurrently, even if they
/// choose different ports. The kernel releases this advisory lease when the app exits;
/// durable sync ownership additionally prevents re-entry while an orphaned uv tree is alive.
public final class RuntimeLease {
    private let descriptor: Int32

    public init(paths: RuntimePaths) throws {
        try FileManager.default.createDirectory(
            at: paths.root, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
        let file = paths.root.appendingPathComponent("installation.lock")
        let fd = open(file.path, O_CREAT | O_RDWR | O_CLOEXEC | O_NOFOLLOW, mode_t(0o600))
        guard fd >= 0 else { throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO) }
        guard flock(fd, LOCK_EX | LOCK_NB) == 0 else {
            close(fd)
            throw RuntimeInstallation.Failure(message: "別の narumi アプリがこの環境を使用しています")
        }
        do {
            try RuntimeSyncOwnership(paths: paths).requireIdle()
        } catch {
            close(fd)
            throw error
        }
        descriptor = fd
    }

    deinit {
        flock(descriptor, LOCK_UN)
        close(descriptor)
    }
}
