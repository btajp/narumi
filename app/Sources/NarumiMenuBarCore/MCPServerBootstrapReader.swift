import Darwin
import Foundation

public protocol MCPServerBootstrapLoading: Sendable {
    func load(expectedURL: URL) throws -> MCPServerConnection
}

/// Descriptor-relative reads keep a symlink or replacement between metadata checks and reads
/// from changing the trusted file. Only public bootstrap metadata is ever read from disk.
public struct MCPServerBootstrapReader: MCPServerBootstrapLoading, Sendable {
    public let dataRoot: URL
    private let secrets: any KeychainSecretReading
    private let processIsRunning: @Sendable (Int32) -> Bool
    private let ownerID: uid_t

    public init(
        dataRoot: URL, secrets: any KeychainSecretReading,
        processIsRunning: @escaping @Sendable (Int32) -> Bool = { kill($0, 0) == 0 }
    ) {
        self.dataRoot = dataRoot
        self.secrets = secrets
        self.processIsRunning = processIsRunning
        ownerID = geteuid()
    }

    init(dataRoot: URL, secrets: any KeychainSecretReading, expectedOwner: uid_t) {
        self.dataRoot = dataRoot
        self.secrets = secrets
        processIsRunning = { _ in true }
        ownerID = expectedOwner
    }

    public func load(expectedURL: URL) throws -> MCPServerConnection {
        _ = try MCPServerEndpoint.validate(expectedURL)
        let bootstrap = try readBootstrap()
        try bootstrap.validate(expectedURL: expectedURL)
        guard processIsRunning(bootstrap.pid) else { throw MCPConnectionError.serverUnavailable }
        let accountRoot = dataRoot.standardizedFileURL.resolvingSymlinksInPath().path
        let rootHash = MCPServerBootstrap.fingerprint(Data(accountRoot.utf8))
        guard bootstrap.tokenAccount == "transport:\(rootHash):\(bootstrap.serverInstanceID)" else {
            throw MCPConnectionError.invalidBootstrap
        }
        do {
            guard let token = try secrets.get(account: bootstrap.tokenAccount) else {
                throw MCPConnectionError.credentialUnavailable
            }
            return try MCPServerConnection(bootstrap: bootstrap, token: token)
        } catch {
            throw MCPConnectionError.credentialUnavailable
        }
    }

    private func readBootstrap() throws -> MCPServerBootstrap {
        let root = open(dataRoot.path, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC)
        guard root >= 0 else { throw openError() }
        defer { close(root) }
        try validateDirectory(root, privateOnly: false)
        let runtime = openat(root, "runtime", O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC)
        guard runtime >= 0 else { throw openError() }
        defer { close(runtime) }
        try validateDirectory(runtime, privateOnly: true)
        let server = openat(runtime, "server", O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC)
        guard server >= 0 else { throw openError() }
        defer { close(server) }
        try validateDirectory(server, privateOnly: true)
        let file = openat(server, "bootstrap.json", O_RDONLY | O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC)
        guard file >= 0 else { throw openError() }
        defer { close(file) }
        var before = stat()
        guard fstat(file, &before) == 0, before.st_uid == ownerID,
            before.st_mode & S_IFMT == S_IFREG, before.st_mode & 0o7777 == 0o600,
            before.st_nlink == 1, (1...65_536).contains(before.st_size)
        else { throw MCPConnectionError.unsafeBootstrap }
        try rejectExtendedAllowACL(file)
        var bytes = [UInt8](repeating: 0, count: Int(before.st_size))
        var received = 0
        while received < bytes.count {
            let count = bytes.withUnsafeMutableBytes { buffer in
                read(file, buffer.baseAddress!.advanced(by: received), buffer.count - received)
            }
            if count < 0, errno == EINTR { continue }
            guard count > 0 else { throw MCPConnectionError.invalidBootstrap }
            received += count
        }
        var after = stat()
        guard fstat(file, &after) == 0,
            before.st_ino == after.st_ino, before.st_dev == after.st_dev,
            before.st_size == after.st_size, before.st_mtimespec.tv_sec == after.st_mtimespec.tv_sec,
            before.st_mtimespec.tv_nsec == after.st_mtimespec.tv_nsec
        else { throw MCPConnectionError.invalidBootstrap }
        do {
            return try JSONDecoder().decode(MCPServerBootstrap.self, from: Data(bytes))
        } catch {
            throw MCPConnectionError.invalidBootstrap
        }
    }

    private func validateDirectory(_ descriptor: Int32, privateOnly: Bool) throws {
        var metadata = stat()
        guard fstat(descriptor, &metadata) == 0, metadata.st_uid == ownerID,
            metadata.st_mode & S_IFMT == S_IFDIR,
            privateOnly ? metadata.st_mode & 0o7777 == 0o700 : metadata.st_mode & 0o022 == 0
        else { throw MCPConnectionError.unsafeBootstrap }
        try rejectExtendedAllowACL(descriptor)
    }

    private func rejectExtendedAllowACL(_ descriptor: Int32) throws {
        guard let acl = acl_get_fd_np(descriptor, ACL_TYPE_EXTENDED) else {
            if errno == ENOENT || errno == ENOATTR { return }
            throw MCPConnectionError.unsafeBootstrap
        }
        defer { acl_free(UnsafeMutableRawPointer(acl)) }
        var entry: acl_entry_t?
        var entryID = Int32(ACL_FIRST_ENTRY.rawValue)
        while true {
            let result = acl_get_entry(acl, entryID, &entry)
            // Darwin returns -1 / EINVAL when there is no next entry, unlike POSIX ACL APIs.
            if result == -1, errno == EINVAL { return }
            guard result == 0, let entry else { throw MCPConnectionError.unsafeBootstrap }
            var tag = ACL_UNDEFINED_TAG
            guard acl_get_tag_type(entry, &tag) == 0, tag != ACL_EXTENDED_ALLOW else {
                throw MCPConnectionError.unsafeBootstrap
            }
            entryID = Int32(ACL_NEXT_ENTRY.rawValue)
        }
    }

    private func openError() -> MCPConnectionError {
        errno == ENOENT ? .bootstrapUnavailable : .unsafeBootstrap
    }
}
