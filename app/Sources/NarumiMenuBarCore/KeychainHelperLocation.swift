import Darwin
import Foundation

/// A helper can be executed only from a known app bundle or the loaded repository's build.
/// The dev release/debug directory links may resolve inside .build; the executable cannot be a link.
public struct KeychainHelperLocation: Equatable, Sendable {
    public let trustedRoot: URL
    public let candidate: URL
    public let buildRoot: URL?

    public init(trustedRoot: URL, candidate: URL, buildRoot: URL? = nil) {
        self.trustedRoot = trustedRoot
        self.candidate = candidate
        self.buildRoot = buildRoot
    }

    public func validatedExecutable() throws -> URL {
        let root = trustedRoot.standardizedFileURL
        let executable = candidate.standardizedFileURL
        guard executable.path.hasPrefix(root.path + "/") else { throw MCPConnectionError.credentialUnavailable }
        if let buildRoot {
            let build = buildRoot.standardizedFileURL
            try validateDirectoryTree(root: root, leaf: build)
            let resolved = executable.deletingLastPathComponent().resolvingSymlinksInPath()
            guard resolved.path.hasPrefix(build.resolvingSymlinksInPath().path + "/") else {
                throw MCPConnectionError.credentialUnavailable
            }
            try validateDirectoryTree(root: build.resolvingSymlinksInPath(), leaf: resolved)
        } else {
            try validateDirectoryTree(root: root, leaf: executable.deletingLastPathComponent())
        }
        let descriptor = open(executable.path, O_RDONLY | O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC)
        guard descriptor >= 0 else { throw MCPConnectionError.credentialUnavailable }
        defer { close(descriptor) }
        var metadata = stat()
        guard fstat(descriptor, &metadata) == 0, metadata.st_mode & S_IFMT == S_IFREG,
            [uid_t(0), geteuid()].contains(metadata.st_uid), metadata.st_mode & 0o022 == 0,
            metadata.st_mode & 0o111 != 0
        else { throw MCPConnectionError.credentialUnavailable }
        try rejectAllowACL(descriptor)
        return executable.resolvingSymlinksInPath()
    }

    private func validateDirectoryTree(root: URL, leaf: URL) throws {
        guard leaf == root || leaf.path.hasPrefix(root.path + "/") else {
            throw MCPConnectionError.credentialUnavailable
        }
        var current = root
        let components = leaf.path.dropFirst(root.path.count).split(separator: "/")
        try validateDirectory(current)
        for component in components {
            current.appendPathComponent(String(component), isDirectory: true)
            try validateDirectory(current)
        }
    }

    private func validateDirectory(_ directory: URL) throws {
        let descriptor = open(directory.path, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC)
        guard descriptor >= 0 else { throw MCPConnectionError.credentialUnavailable }
        defer { close(descriptor) }
        var metadata = stat()
        guard fstat(descriptor, &metadata) == 0, metadata.st_mode & S_IFMT == S_IFDIR,
            [uid_t(0), geteuid()].contains(metadata.st_uid), metadata.st_mode & 0o022 == 0
        else { throw MCPConnectionError.credentialUnavailable }
        try rejectAllowACL(descriptor)
    }

    private func rejectAllowACL(_ descriptor: Int32) throws {
        guard let acl = acl_get_fd_np(descriptor, ACL_TYPE_EXTENDED) else {
            if errno == ENOENT || errno == ENOATTR { return }
            throw MCPConnectionError.credentialUnavailable
        }
        defer { acl_free(UnsafeMutableRawPointer(acl)) }
        var entry: acl_entry_t?
        var entryID = Int32(ACL_FIRST_ENTRY.rawValue)
        while true {
            let result = acl_get_entry(acl, entryID, &entry)
            if result == -1, errno == EINVAL { return }
            guard result == 0, let entry else { throw MCPConnectionError.credentialUnavailable }
            var tag = ACL_UNDEFINED_TAG
            guard acl_get_tag_type(entry, &tag) == 0, tag != ACL_EXTENDED_ALLOW else {
                throw MCPConnectionError.credentialUnavailable
            }
            entryID = Int32(ACL_NEXT_ENTRY.rawValue)
        }
    }
}
