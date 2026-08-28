import Foundation
import XCTest

/// Executes the real app actor with injected bootstrap and HTTP implementations. The Core
/// transport tests separately exercise real TLS; this fixture never launches the GUI or a provider.
final class MCPClientSecureSessionTests: XCTestCase {
    func testRealClientAuthenticatesBeforeToolsRejectsDowngradeAndNeverReplaysSecrets() throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent("narumi-client-test-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: root) }
        let source = root.appendingPathComponent("Fixture.swift")
        try Data(MCPClientSessionFixtureSource.text.utf8).write(to: source)
        let app = URL(fileURLWithPath: #filePath).resolvingSymlinksInPath()
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        let core = app.appendingPathComponent("Sources/NarumiMenuBarCore")
        let coreFiles = try FileManager.default.contentsOfDirectory(at: core, includingPropertiesForKeys: nil)
            .filter { $0.pathExtension == "swift" }.sorted { $0.path < $1.path }
        let executable = root.appendingPathComponent("fixture")
        let appSources = ["MCPClient.swift", "MCPClient+Connection.swift", "MCPClient+JobRecovery.swift", "JSONNode.swift"]
            .map { app.appendingPathComponent("Sources/NarumiMenuBar/\($0)").path }
        let compiler = Process()
        compiler.executableURL = URL(fileURLWithPath: "/usr/bin/xcrun")
        compiler.arguments = ["swiftc", "-swift-version", "6", "-parse-as-library", "-module-name", "NarumiMenuBarCore",
            "-o", executable.path, source.path,
            app.appendingPathComponent("Tests/NarumiMenuBarCoreTests/LoopbackTLSCertificate.swift").path]
            + appSources + coreFiles.map(\.path)
        let compileOutput = try run(compiler)
        XCTAssertEqual(compiler.terminationStatus, 0, compileOutput)
        guard compiler.terminationStatus == 0 else { return }
        let fixture = Process()
        fixture.executableURL = executable
        fixture.arguments = [root.path]
        let result = try run(fixture)
        XCTAssertEqual(fixture.terminationStatus, 0, result)
        let checks = try JSONDecoder().decode([String: Bool].self, from: Data(result.utf8))
        XCTAssertEqual(Set(checks.keys), [
            "bootstrap_before_http", "discovery_before_tools", "v1_rejected", "missing_tls_rejected",
            "false_client_auth_rejected", "wrong_instance_rejected", "secret_not_replayed",
            "setup_reconciled_without_replay", "readonly_reconnect", "rpc_error_redacted",
            "malformed_response_redacted", "invalid_config_rejected",
        ])
        for (name, passed) in checks { XCTAssertTrue(passed, name) }
    }

    private func run(_ process: Process) throws -> String {
        let output = Pipe()
        process.standardOutput = output
        process.standardError = output
        process.standardInput = FileHandle.nullDevice
        try process.run()
        let data = output.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        return String(decoding: data, as: UTF8.self)
    }
}
