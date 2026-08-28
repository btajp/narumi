import XCTest

@testable import NarumiMenuBarCore

final class ServerReadinessTests: XCTestCase {
    private let identity = BundledServerIdentity(
        serverVersion: "2.0.0", contractVersion: "2.0.0",
        recorder: URL(fileURLWithPath: "/Applications/narumi.app/Contents/MacOS/narumi-recorder"),
        contractsDirectory: URL(fileURLWithPath: "/Applications/narumi.app/Contents/Resources/runtime/contracts"),
        dataRoot: URL(fileURLWithPath: "/Users/tester/Library/Application Support/narumi"))

    private func response() throws -> ServerInfo {
        try JSONDecoder().decode(ServerInfo.self, from: Data("""
            {
              "name": "narumi", "server_version": "2.0.0", "contract_version": "2.0.0",
              "secure_transport": {
                "mode": "pinned_tls", "tls_required": true, "client_auth_required": true
              },
              "capabilities": {
                "recording": false, "transports": ["streamable-http"],
                "transcription_engines": ["fake"], "diarization_engines": ["none"],
                "llm_providers": ["none"], "export_destinations": ["markdown"]
              },
              "diagnostics": {
                "ffmpeg": null, "ffprobe": null,
                "data_root": "/Users/tester/Library/Application Support/narumi",
                "meetings_root": "/Users/tester/Library/Application Support/narumi/meetings",
                "catalog_path": "/Users/tester/Library/Application Support/narumi/narumi.db",
                "recorder_path": "/Applications/narumi.app/Contents/MacOS/narumi-recorder",
                "contracts_dir": "/Applications/narumi.app/Contents/Resources/runtime/contracts"
              }
            }
            """.utf8))
    }

    func testMatchingBundleIsReadyEvenIfRecordingPermissionIsNotYetGranted() throws {
        XCTAssertNoThrow(try identity.validate(response()))
    }

    func testMismatchedVersionOrDiagnosticsAreRejected() throws {
        let mutations: [(String, (inout ServerInfo) -> Void)] = [
            ("name", { $0.name = "other" }),
            ("server_version", { $0.serverVersion = "1.0.0" }),
            ("contract_version", { $0.contractVersion = "2.1.0" }),
            ("recorder_path", { $0.diagnostics.recorderPath = nil }),
            ("recorder_path", { $0.diagnostics.recorderPath = "/old/dist/narumi.app/Contents/MacOS/narumi-recorder" }),
            ("recorder_path", { $0.diagnostics.recorderPath = "narumi-recorder" }),
            ("contracts_dir", { $0.diagnostics.contractsDir = "/old/checkout/contracts" }),
            ("data_root", { $0.diagnostics.dataRoot = "/another/user/data" }),
        ]
        for (field, mutate) in mutations {
            var info = try response()
            mutate(&info)
            XCTAssertThrowsError(try identity.validate(info)) { error in
                XCTAssertEqual((error as? BundledServerIdentity.Mismatch)?.field, field)
            }
        }
    }

    func testUnsupportedContractCannotBeAdoptedEvenWhenBundleVersionMatches() throws {
        for version in ["1.0.0", "1.1.0", "2.0.0-rc.1", "3.0.0-rc.1", "4.0.0", "malformed"] {
            var info = try response()
            info.contractVersion = version
            var expected = identity
            expected.contractVersion = version
            XCTAssertThrowsError(try expected.validate(info), version) { error in
                XCTAssertEqual(error as? MCPConnectionError, .incompatibleContract)
            }
        }
    }

    func testSecureTransportRequiresPinnedTLSAndClientAuthentication() throws {
        let metadata: [SecureTransportMetadata?] = [
            nil,
            .init(mode: "stdio", tlsRequired: true, clientAuthRequired: true),
            .init(mode: "http", tlsRequired: false, clientAuthRequired: false),
            .init(mode: "pinned_tls", tlsRequired: false, clientAuthRequired: true),
            .init(mode: "pinned_tls", tlsRequired: true, clientAuthRequired: false),
            .init(mode: "unknown", tlsRequired: true, clientAuthRequired: true),
        ]
        for transport in metadata {
            var info = try response()
            info.secureTransport = transport
            XCTAssertThrowsError(try identity.validate(info)) { error in
                XCTAssertEqual(error as? MCPConnectionError, .incompatibleContract)
            }
        }
    }

    func testPathComparisonUsesActualFilesThroughSymlinks() throws {
        let root = try temporaryDirectory()
        let actual = root.appendingPathComponent("actual")
        let alias = root.appendingPathComponent("alias")
        try FileManager.default.createDirectory(at: actual, withIntermediateDirectories: true)
        try FileManager.default.createSymbolicLink(at: alias, withDestinationURL: actual)
        var expected = identity
        expected.recorder = alias.appendingPathComponent("narumi-recorder")
        try Data("fake recorder".utf8).write(to: actual.appendingPathComponent("narumi-recorder"))
        var info = try response()
        info.diagnostics.recorderPath = actual.appendingPathComponent("narumi-recorder").path
        XCTAssertNoThrow(try expected.validate(info))
        info.diagnostics.recorderPath = root.appendingPathComponent("different-recorder").path
        XCTAssertThrowsError(try expected.validate(info))
    }

    func testIdentityLoadsExpectedVersionAndContractsFromSelectedBundle() throws {
        let root = try temporaryDirectory()
        let bundle = root.appendingPathComponent("narumi.app")
        let runtime = BundledRuntime(root: bundle.appendingPathComponent(BundledRuntime.bundleSubpath))
        let recorder = bundle.appendingPathComponent(ServerConfig.recorderPathInBundle)
        try FileManager.default.createDirectory(at: recorder.deletingLastPathComponent(), withIntermediateDirectories: true)
        try FileManager.default.createDirectory(at: runtime.contractsDir, withIntermediateDirectories: true)
        try Data("fake recorder".utf8).write(to: recorder)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: recorder.path)
        try Data("{\"contract_version\":\"3.2.1\"}".utf8)
            .write(to: runtime.contractsDir.appendingPathComponent("manifest.json"))
        let config = ServerConfig.resolve(
            environment: ["NARUMI_HOME": root.appendingPathComponent("data").path],
            storedRepoPath: "/stale/repo", bundleURL: bundle, homeDirectory: root)
        let manifest = RuntimeManifest(
            appVersion: "2.0.0", python: "3.13", uvVersion: "0.12.6", wheels: [:], requirementsSHA256: "abc")
        let expected = try BundledServerIdentity.load(config: config, manifest: manifest)
        XCTAssertEqual(expected.serverVersion, "2.0.0")
        XCTAssertEqual(expected.contractVersion, "3.2.1")
        XCTAssertEqual(expected.recorder.path, recorder.path)
        XCTAssertEqual(expected.contractsDirectory.path, runtime.contractsDir.path)
        XCTAssertEqual(expected.dataRoot.path, root.appendingPathComponent("data").path)
        try FileManager.default.setAttributes([.posixPermissions: 0o644], ofItemAtPath: recorder.path)
        XCTAssertThrowsError(try BundledServerIdentity.load(config: config, manifest: manifest))
    }

    func testFailedIdentityCheckLeavesOldMarkerAndCanRollBack() throws {
        let paths = RuntimePaths(dataRoot: try temporaryDirectory())
        try FileManager.default.createDirectory(at: paths.venv, withIntermediateDirectories: true)
        try FileManager.default.createDirectory(at: paths.venvStaging, withIntermediateDirectories: true)
        try Data("old environment".utf8).write(to: paths.venv.appendingPathComponent("old"))
        let oldMarker = Data("old marker".utf8)
        try oldMarker.write(to: paths.installedManifest)
        let candidate = RuntimeManifest(
            appVersion: "2.0.0", python: "3.13", uvVersion: "0.12.6", wheels: [:], requirementsSHA256: "abc")
        let installation = RuntimeInstallation(paths: paths)
        try installation.activate(manifest: JSONEncoder().encode(candidate))
        var info = try response()
        info.serverVersion = "1.0.0"
        XCTAssertThrowsError(try identity.validate(info))
        XCTAssertEqual(try Data(contentsOf: paths.installedManifest), oldMarker)
        XCTAssertEqual(try installation.recover(), .rolledBack)
        XCTAssertTrue(FileManager.default.fileExists(atPath: paths.venv.appendingPathComponent("old").path))
        XCTAssertEqual(try Data(contentsOf: paths.installedManifest), oldMarker)
    }

    private func temporaryDirectory() throws -> URL {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent("narumi-readiness-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: root) }
        return root
    }
}
