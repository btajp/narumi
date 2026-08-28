import Darwin
import Foundation
import XCTest

@testable import NarumiMenuBarCore

@MainActor
final class MCPServerBootstrapReaderTests: XCTestCase {
    private let endpoint = URL(string: "https://127.0.0.1:8765/mcp")!
    private let token = "narumi-bootstrap-test-token-never-production"
    private let instance = "00000000-0000-4000-8000-000000000001"

    func testOwnerControlledBootstrapLoadsOnlyItsMatchingKeychainAccount() throws {
        let fixture = try makeFixture()
        defer { try? FileManager.default.removeItem(at: fixture.root) }
        let secrets = FakeSecrets(value: token)
        let reader = MCPServerBootstrapReader(dataRoot: fixture.root, secrets: secrets)
        let connection = try reader.load(expectedURL: endpoint)
        XCTAssertEqual(connection.bootstrap, fixture.bootstrap)
        XCTAssertEqual(secrets.accounts, [fixture.bootstrap.tokenAccount])
        XCTAssertFalse(String(describing: connection).contains(token))
        XCTAssertFalse(String(reflecting: connection).contains(token))
    }

    func testUnsafeFileAndDirectoryPermissionsRejectBeforeCredentialRead() throws {
        for part in ["", "runtime", "runtime/server", "runtime/server/bootstrap.json"] {
            let fixture = try makeFixture()
            defer { try? FileManager.default.removeItem(at: fixture.root) }
            let path = fixture.root.appendingPathComponent(part)
            let mode: mode_t = part.hasSuffix("json") ? 0o644 : (part.isEmpty ? 0o775 : 0o755)
            XCTAssertEqual(chmod(path.path, mode), 0)
            let secrets = FakeSecrets(value: token)
            let reader = MCPServerBootstrapReader(dataRoot: fixture.root, secrets: secrets)
            XCTAssertThrowsError(try reader.load(expectedURL: endpoint), part) {
                XCTAssertEqual($0 as? MCPConnectionError, .unsafeBootstrap)
            }
            XCTAssertTrue(secrets.accounts.isEmpty)
        }
    }

    func testDifferentOwnerRejectsBeforeCredentialRead() throws {
        let fixture = try makeFixture()
        defer { try? FileManager.default.removeItem(at: fixture.root) }
        let secrets = FakeSecrets(value: token)
        let reader = MCPServerBootstrapReader(dataRoot: fixture.root, secrets: secrets, expectedOwner: geteuid() + 1)
        XCTAssertThrowsError(try reader.load(expectedURL: endpoint)) {
            XCTAssertEqual($0 as? MCPConnectionError, .unsafeBootstrap)
        }
        XCTAssertTrue(secrets.accounts.isEmpty)
    }

    func testExtendedAllowACLIsRejectedEvenWhenModeIsOwnerOnly() throws {
        let fixture = try makeFixture()
        defer { try? FileManager.default.removeItem(at: fixture.root) }
        let file = fixture.root.appendingPathComponent("runtime/server/bootstrap.json")
        let descriptor = open(file.path, O_RDONLY | O_NOFOLLOW)
        guard descriptor >= 0 else { throw CocoaError(.fileReadUnknown) }
        defer { close(descriptor) }
        var acl: acl_t? = try XCTUnwrap(acl_init(1))
        defer { if let acl { acl_free(UnsafeMutableRawPointer(acl)) } }
        var entry: acl_entry_t?
        XCTAssertEqual(acl_create_entry(&acl, &entry), 0)
        let created = try XCTUnwrap(entry)
        XCTAssertEqual(acl_set_tag_type(created, ACL_EXTENDED_ALLOW), 0)
        var principal = UUID().uuid
        XCTAssertEqual(withUnsafePointer(to: &principal) { acl_set_qualifier(created, $0) }, 0)
        var permissions: acl_permset_t?
        XCTAssertEqual(acl_get_permset(created, &permissions), 0)
        XCTAssertEqual(acl_add_perm(try XCTUnwrap(permissions), ACL_READ_DATA), 0)
        XCTAssertEqual(acl_set_fd_np(descriptor, try XCTUnwrap(acl), ACL_TYPE_EXTENDED), 0)
        let secrets = FakeSecrets(value: token)
        let reader = MCPServerBootstrapReader(dataRoot: fixture.root, secrets: secrets)
        XCTAssertThrowsError(try reader.load(expectedURL: endpoint)) {
            XCTAssertEqual($0 as? MCPConnectionError, .unsafeBootstrap)
        }
        XCTAssertTrue(secrets.accounts.isEmpty)
    }

    func testFileAndDirectorySymlinksAreNotFollowed() throws {
        for part in ["runtime", "runtime/server", "runtime/server/bootstrap.json"] {
            let fixture = try makeFixture()
            defer { try? FileManager.default.removeItem(at: fixture.root) }
            let original = fixture.root.appendingPathComponent(part)
            let target = fixture.root.appendingPathComponent("target")
            try FileManager.default.moveItem(at: original, to: target)
            try FileManager.default.createSymbolicLink(at: original, withDestinationURL: target)
            let secrets = FakeSecrets(value: token)
            let reader = MCPServerBootstrapReader(dataRoot: fixture.root, secrets: secrets)
            XCTAssertThrowsError(try reader.load(expectedURL: endpoint), part) {
                XCTAssertEqual($0 as? MCPConnectionError, .unsafeBootstrap)
            }
            XCTAssertTrue(secrets.accounts.isEmpty)
        }
    }

    func testRootSymlinkIsRejected() throws {
        let fixture = try makeFixture()
        defer { try? FileManager.default.removeItem(at: fixture.root) }
        let alias = fixture.root.deletingLastPathComponent().appendingPathComponent("narumi-link-\(UUID().uuidString)")
        defer { try? FileManager.default.removeItem(at: alias) }
        try FileManager.default.createSymbolicLink(at: alias, withDestinationURL: fixture.root)
        let secrets = FakeSecrets(value: token)
        let reader = MCPServerBootstrapReader(dataRoot: alias, secrets: secrets)
        XCTAssertThrowsError(try reader.load(expectedURL: endpoint)) {
            XCTAssertEqual($0 as? MCPConnectionError, .unsafeBootstrap)
        }
        XCTAssertTrue(secrets.accounts.isEmpty)
    }

    func testHardlinkAndFIFOAreRejectedWithoutBlocking() throws {
        for useFIFO in [false, true] {
            let fixture = try makeFixture()
            defer { try? FileManager.default.removeItem(at: fixture.root) }
            let file = fixture.root.appendingPathComponent("runtime/server/bootstrap.json")
            if useFIFO {
                try FileManager.default.removeItem(at: file)
                XCTAssertEqual(mkfifo(file.path, 0o600), 0)
            } else {
                XCTAssertEqual(link(file.path, fixture.root.appendingPathComponent("linked.json").path), 0)
            }
            let secrets = FakeSecrets(value: token)
            let reader = MCPServerBootstrapReader(dataRoot: fixture.root, secrets: secrets)
            XCTAssertThrowsError(try reader.load(expectedURL: endpoint)) {
                XCTAssertEqual($0 as? MCPConnectionError, .unsafeBootstrap)
            }
            XCTAssertTrue(secrets.accounts.isEmpty)
        }
    }

    func testMalformedIdentityCertificateAndCredentialNamespaceCannotReadTokens() throws {
        let mutations: [(inout [String: Any]) -> Void] = [
            { $0["version"] = 2 }, { $0["pid"] = -1 }, { $0["server_instance_id"] = "invalid" },
            { $0["certificate_sha256"] = String(repeating: "f", count: 64) },
            { $0["certificate_pem"] = "-----BEGIN CERTIFICATE-----\ninvalid\n-----END CERTIFICATE-----" },
            { $0["token_account"] = "provider:another-connection" },
            { $0["token_account"] = "transport:\(String(repeating: "f", count: 64)):00000000-0000-4000-8000-000000000001" },
            { $0["url"] = "http://127.0.0.1:8765/mcp" },
            { $0["url"] = "https://127.0.0.1:9876/mcp" },
        ]
        for mutate in mutations {
            let fixture = try makeFixture()
            defer { try? FileManager.default.removeItem(at: fixture.root) }
            let file = fixture.root.appendingPathComponent("runtime/server/bootstrap.json")
            var values = try XCTUnwrap(JSONSerialization.jsonObject(with: Data(contentsOf: file)) as? [String: Any])
            mutate(&values)
            try JSONSerialization.data(withJSONObject: values).write(to: file)
            let secrets = FakeSecrets(value: token)
            let reader = MCPServerBootstrapReader(dataRoot: fixture.root, secrets: secrets)
            XCTAssertThrowsError(try reader.load(expectedURL: endpoint))
            XCTAssertTrue(secrets.accounts.isEmpty)
        }
    }

    func testDeadProcessAndMissingBootstrapNeverReadCredentials() throws {
        let fixture = try makeFixture()
        defer { try? FileManager.default.removeItem(at: fixture.root) }
        let secrets = FakeSecrets(value: token)
        let reader = MCPServerBootstrapReader(dataRoot: fixture.root, secrets: secrets, processIsRunning: { _ in false })
        XCTAssertThrowsError(try reader.load(expectedURL: endpoint)) {
            XCTAssertEqual($0 as? MCPConnectionError, .serverUnavailable)
        }
        try FileManager.default.removeItem(at: fixture.root.appendingPathComponent("runtime/server/bootstrap.json"))
        XCTAssertThrowsError(try reader.load(expectedURL: endpoint)) {
            XCTAssertEqual($0 as? MCPConnectionError, .bootstrapUnavailable)
        }
        XCTAssertTrue(secrets.accounts.isEmpty)
    }

    func testMalformedTokenAndCredentialErrorsAreRedacted() throws {
        let fixture = try makeFixture()
        defer { try? FileManager.default.removeItem(at: fixture.root) }
        for value in [nil, "", "short", "test-header-injection\r\nAuthorization: stolen", "test secret with spaces"] {
            let reader = MCPServerBootstrapReader(dataRoot: fixture.root, secrets: FakeSecrets(value: value))
            XCTAssertThrowsError(try reader.load(expectedURL: endpoint)) {
                XCTAssertEqual($0 as? MCPConnectionError, .credentialUnavailable)
                if let value, !value.isEmpty { XCTAssertFalse($0.localizedDescription.contains(value)) }
            }
        }
        let reader = MCPServerBootstrapReader(dataRoot: fixture.root, secrets: FakeSecrets(value: token, fail: true))
        XCTAssertThrowsError(try reader.load(expectedURL: endpoint)) {
            XCTAssertEqual($0 as? MCPConnectionError, .credentialUnavailable)
            XCTAssertFalse($0.localizedDescription.contains("credential-helper-private-error"))
        }
    }

    private func makeFixture() throws -> (root: URL, bootstrap: MCPServerBootstrap) {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent("narumi-bootstrap-\(UUID().uuidString)")
        for directory in [root, root.appendingPathComponent("runtime"), root.appendingPathComponent("runtime/server")] {
            try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: false,
                attributes: [.posixPermissions: 0o700])
        }
        let accountRoot = root.standardizedFileURL.resolvingSymlinksInPath().path
        let rootHash = MCPServerBootstrap.fingerprint(Data(accountRoot.utf8))
        let bootstrap = MCPServerBootstrap(
            serverInstanceID: instance, pid: getpid(), url: endpoint,
            certificateSHA256: MCPServerBootstrap.fingerprint(LoopbackTLSCertificate.certificateDER),
            certificatePEM: LoopbackTLSCertificate.certificatePEM, tokenAccount: "transport:\(rootHash):\(instance)")
        let file = root.appendingPathComponent("runtime/server/bootstrap.json")
        try JSONEncoder().encode(bootstrap).write(to: file)
        guard chmod(file.path, 0o600) == 0 else { throw CocoaError(.fileWriteNoPermission) }
        return (root, bootstrap)
    }

    private final class FakeSecrets: KeychainSecretReading, @unchecked Sendable {
        private let lock = NSLock()
        private var reads: [String] = []
        private let value: String?
        private let fail: Bool
        var accounts: [String] { lock.withLock { reads } }

        init(value: String?, fail: Bool = false) { self.value = value; self.fail = fail }

        func get(account: String) throws -> String? {
            lock.withLock { reads.append(account) }
            if fail { throw NSError(domain: "credential-helper-private-error", code: 1) }
            return value
        }
    }
}
