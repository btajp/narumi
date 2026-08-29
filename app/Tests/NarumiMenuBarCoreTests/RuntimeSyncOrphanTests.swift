import Darwin
import XCTest

@testable import NarumiMenuBarCore

/// Real subprocesses, but no GUI app, uv, Python installation, HTTP or user data. A tiny
/// fixture uses the production ownership helper and actual ServerLauncher with a fake MCP
/// client; all paths and the briefly bound loopback port are isolated to the test.
final class RuntimeSyncOrphanTests: XCTestCase {
    private struct Timeout: Error {}

    func testCrashedAppCannotClearStagingWhileUVOrDescendantsRemain() throws {
        try assertCrashRecovery(modes: ["uv", "grandchild", "pending", "fast"])
    }

    func testLauncherStartChecksOrphanOwnershipBeforeRecoveringPendingInstallation() throws {
        try assertCrashRecovery(modes: ["server"])
    }

    private func assertCrashRecovery(modes: [String]) throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent("narumi-orphan-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: root) }
        let executable = try compileFixture(in: root)
        for mode in modes {
            let data = root.appendingPathComponent(mode)
            let paths = RuntimePaths(dataRoot: data)
            try FileManager.default.createDirectory(at: paths.venvStaging, withIntermediateDirectories: true)
            let sentinel = paths.venvStaging.appendingPathComponent("sentinel")
            try Data("original staging".utf8).write(to: sentinel)
            try FileManager.default.createDirectory(at: paths.venv, withIntermediateDirectories: true)
            try Data("old runtime".utf8).write(to: paths.venv.appendingPathComponent("sentinel"))
            try Data("old marker".utf8).write(to: paths.installedManifest)
            let fakeUV = data.appendingPathComponent("fake-uv.sh")
            try Data(Self.fakeUV.utf8).write(to: fakeUV)
            let app = try launch(executable, arguments: [data.path, mode])
            defer {
                try? Data().write(to: data.appendingPathComponent("finish"))
                if app.isRunning { kill(app.processIdentifier, SIGKILL) }
                try? wait { !app.isRunning }
                // The fake children check finish every 20 ms; allow them to exit before
                // deleting the test directory that contains their completion signal.
                Thread.sleep(forTimeInterval: 0.1)
            }
            let ready: String
            switch mode {
            case "grandchild": ready = "parent-blocked"
            case "pending": ready = "pending-ready"
            case "fast": ready = "parent-done"
            default: ready = "uv-ready"
            }
            try wait { FileManager.default.fileExists(atPath: data.appendingPathComponent(ready).path) }
            let ownerURL = paths.root.appendingPathComponent("sync-owner.json")
            let ownerData = try? Data(contentsOf: ownerURL)
            let record = ownerData.flatMap { try? JSONDecoder().decode(RuntimeSyncOwnership.Record.self, from: $0) }
            let journalData = try? Data(contentsOf: paths.transactionJournal)
            // SIGKILL only this test's fixture app; the guard must never kill its orphan.
            XCTAssertEqual(kill(app.processIdentifier, SIGKILL), 0)
            try wait { !app.isRunning }
            app.waitUntilExit()
            // Server recovery must exercise the actual Launcher.start() call order, not a
            // fixture that performs requireIdle()/lease/recover on the launcher's behalf.
            let retryMode = mode == "server" ? "launcher-retry" : "retry"
            let retry = try launch(executable, arguments: [data.path, retryMode])
            try wait { !retry.isRunning }
            retry.waitUntilExit()
            if mode == "fast" {
                XCTAssertEqual(retry.terminationStatus, 0)
                XCTAssertEqual(try Data(contentsOf: sentinel), Data("new staging".utf8))
                continue
            }
            XCTAssertEqual(retry.terminationStatus, 2, mode)
            let activeSentinel = mode == "server" ? paths.venv.appendingPathComponent("sentinel") : sentinel
            XCTAssertEqual(try Data(contentsOf: activeSentinel), Data("original staging".utf8), mode)
            XCTAssertTrue(FileManager.default.fileExists(atPath: ownerURL.path), mode)
            if mode == "server" {
                XCTAssertEqual(try Data(contentsOf: paths.venvPrevious.appendingPathComponent("sentinel")), Data("old runtime".utf8))
                XCTAssertEqual(try Data(contentsOf: paths.installedManifest), Data("old marker".utf8))
                XCTAssertEqual(try Data(contentsOf: ownerURL), try XCTUnwrap(ownerData))
                XCTAssertEqual(try Data(contentsOf: paths.transactionJournal), try XCTUnwrap(journalData))
                let failure = try String(contentsOf: data.appendingPathComponent("launcher-error"), encoding: .utf8)
                XCTAssertTrue(failure.contains("前回の"), failure)
                XCTAssertFalse(failure.contains("manifest 読み込み"), "ownership must fail before recovery/manifest loading")
            }
            if let child = record?.child {
                XCTAssertTrue(try RuntimeSyncOwnership.processGroupExists(child.processGroup), "guard did not stop the orphan")
                try Data().write(to: data.appendingPathComponent("finish"))
                try wait { (try? RuntimeSyncOwnership.processGroupExists(child.processGroup)) == false }
                let afterExit = try launch(executable, arguments: [data.path, retryMode])
                try wait { !afterExit.isRunning }
                afterExit.waitUntilExit()
                XCTAssertEqual(afterExit.terminationStatus, mode == "server" ? 2 : 0)
                XCTAssertFalse(FileManager.default.fileExists(atPath: ownerURL.path))
                if mode == "server" {
                    XCTAssertEqual(try Data(contentsOf: activeSentinel), Data("old runtime".utf8))
                    XCTAssertFalse(FileManager.default.fileExists(atPath: paths.transactionJournal.path))
                    let failure = try String(contentsOf: data.appendingPathComponent("launcher-error"), encoding: .utf8)
                    XCTAssertTrue(failure.contains("manifest 読み込み"), "only after safe recovery may the launcher read the fixture manifest")
                }
            } else {
                XCTAssertEqual(mode, "pending", "normal launches must persist the identity before uv can run")
                XCTAssertFalse(FileManager.default.fileExists(atPath: data.appendingPathComponent("uv-ready").path))
            }
        }
    }

    private func launch(_ executable: URL, arguments: [String]) throws -> Process {
        let process = Process()
        process.executableURL = executable
        process.arguments = arguments
        process.environment = ["PATH": "/usr/bin:/bin"]
        process.standardInput = FileHandle.nullDevice
        process.standardOutput = FileHandle.nullDevice
        process.standardError = FileHandle.nullDevice
        try process.run()
        return process
    }

    private func wait(_ condition: () -> Bool) throws {
        let deadline = Date().addingTimeInterval(10)
        while !condition() {
            guard Date() < deadline else { throw Timeout() }
            Thread.sleep(forTimeInterval: 0.02)
        }
    }

    private func compileFixture(in root: URL) throws -> URL {
        let source = root.appendingPathComponent("Fixture.swift")
        let executable = root.appendingPathComponent("fake-app")
        try Data(Self.fakeApp.utf8).write(to: source)
        let core = URL(fileURLWithPath: #filePath).resolvingSymlinksInPath()
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
            .appendingPathComponent("Sources/NarumiMenuBarCore")
        let compiler = Process()
        compiler.executableURL = URL(fileURLWithPath: "/usr/bin/xcrun")
        compiler.currentDirectoryURL = root
        // Compile the real launcher unchanged with its Foundation-only dependencies. The
        // module name makes its Core import a no-op; sources below must track new launcher
        // dependencies. No SwiftPM .build module, GUI entry point or Sparkle is used.
        compiler.arguments = [
            "swiftc", "-swift-version", "6", "-parse-as-library", "-module-name", "NarumiMenuBarCore",
            "-o", executable.path, source.path,
            core.deletingLastPathComponent().appendingPathComponent("NarumiMenuBar/ServerLauncher.swift").path,
        ]
            + [
                "RuntimeSyncOwnership.swift", "RuntimeGuards.swift", "RuntimeInstallation.swift", "RuntimeSyncPlan.swift", "RuntimeManifest.swift",
                "ServerCommand.swift", "ServerConfig.swift", "ServerReadiness.swift", "ServerState.swift", "OwnedServerRecovery.swift",
                "ContractModels.swift", "ContractVersionedModels.swift", "MinutesModelSelection.swift", "TranscriptionModelSelection.swift",
                "ContractKeyValidation.swift", "MinutesEnsembleSelection.swift", "MinutesEnsembleCapabilities.swift",
                "MinutesModelCapabilities.swift",
                "ProcessingRunModels.swift", "ProcessingRunValidation.swift", "ProcessingArtifactModels.swift", "MinutesRetry.swift",
                "TranscriptionRetry.swift", "ToolErrorInfo.swift", "RecordingPermissionModels.swift", "ToolCatalog.swift",
                "MCPServerEndpoint.swift", "ProviderWorkflowCapabilities.swift", "KeychainHelperLocation.swift",
            ]
                .map { core.appendingPathComponent($0).path }
        let output = Pipe()
        compiler.standardOutput = output
        compiler.standardError = output
        try compiler.run()
        let messages = output.fileHandleForReading.readDataToEndOfFile()
        compiler.waitUntilExit()
        XCTAssertEqual(compiler.terminationStatus, 0, String(decoding: messages, as: UTF8.self))
        guard compiler.terminationStatus == 0 else { throw Timeout() }
        return executable
    }

    private static let fakeUV = """
        if [ "$1" = grandchild ]; then
            /bin/sh "$0" child "$2" &
            exit 0
        fi
        if [ "$1" = fast ]; then exit 0; fi
        /usr/bin/touch "$2/uv-ready"
        while [ ! -f "$2/finish" ]; do /bin/sleep 0.02; done
        """

    private static let fakeApp = """
        import Darwin
        import Foundation
        struct FakeToolResult: Sendable {
            let structuredContent: FakeContent?
        }
        struct FakeContent: Sendable {
            func serialized() -> Data { Data() }
        }
        actor MCPClient {
            func configure(_ config: ServerConfig) throws {
                try config.validateSecureEndpoint()
            }
            func prepareConnection(expectedProcessID: Int32? = nil, expectedProcessGroup: Int32? = nil) throws {
                fatalError("The orphan recovery test must never prepare an MCP connection")
            }
            func callTool(_ name: String, arguments: [String: String]) -> FakeToolResult {
                fatalError("The orphan recovery test must never call MCP")
            }
            func reset() {}
        }
        @main struct FakeApp {
            @MainActor static func main() async {
                do {
                    let data = URL(fileURLWithPath: CommandLine.arguments[1])
                    let mode = CommandLine.arguments[2]
                    let paths = RuntimePaths(dataRoot: data)
                    if mode == "launcher-retry" {
                        // Intentionally do not acquire a lease or inspect ownership here.
                        // The real launcher must guard recovery, including before HTTP bind.
                        let port = try unusedPort()
                        let config = ServerConfig(
                            repository: nil, repositorySource: nil, port: port,
                            serverURL: URL(string: "https://127.0.0.1:\\(port)/mcp")!, recorder: nil,
                            logFile: data.appendingPathComponent("server.log"), dataRoot: data.path,
                            runtimeMode: .bundled, bundledRuntime: BundledRuntime(root: data.appendingPathComponent("empty-bundle")),
                            runtimePaths: paths, runtimeLogFile: data.appendingPathComponent("runtime.log"))
                        let launcher = ServerLauncher(config: config, client: MCPClient())
                        await launcher.start()
                        guard case .failed(let message) = launcher.state else { exit(3) }
                        try Data(message.utf8).write(to: data.appendingPathComponent("launcher-error"))
                        exit(2)
                    }
                    let lease = try RuntimeLease(paths: paths)
                    try withExtendedLifetime(lease) {
                        let ownership = RuntimeSyncOwnership(paths: paths)
                        if mode == "retry" {
                            try ownership.requireIdle()
                            try RuntimeInstallation(paths: paths).recover()
                            if FileManager.default.fileExists(atPath: paths.venvStaging.path) {
                                try FileManager.default.removeItem(at: paths.venvStaging)
                            }
                            try FileManager.default.createDirectory(at: paths.venvStaging, withIntermediateDirectories: true)
                            try Data("new staging".utf8).write(to: paths.venvStaging.appendingPathComponent("sentinel"))
                            return
                        }
                        if mode == "server" {
                            let manifest = RuntimeManifest(
                                appVersion: "2.0.0", python: "3.13", uvVersion: "0.12.6",
                                wheels: [:], requirementsSHA256: "fixture")
                            try RuntimeInstallation(paths: paths).activate(manifest: JSONEncoder().encode(manifest))
                        }
                        if mode == "pending" {
                            ownership.checkpoint = { point in
                                if point == .childLaunched {
                                    try Data().write(to: data.appendingPathComponent("pending-ready"))
                                    while true { Thread.sleep(forTimeInterval: 0.02) }
                                }
                            }
                        }
                        let uv = Process()
                        uv.executableURL = URL(fileURLWithPath: "/bin/sh")
                        uv.arguments = [data.appendingPathComponent("fake-uv.sh").path, mode, data.path]
                        uv.standardOutput = FileHandle.nullDevice
                        uv.standardError = FileHandle.nullDevice
                        try ownership.start(uv)
                        while uv.isRunning { Thread.sleep(forTimeInterval: 0.02) }
                        uv.waitUntilExit()
                        do {
                            try ownership.finish()
                            try Data().write(to: data.appendingPathComponent("parent-done"))
                        } catch {
                            try Data().write(to: data.appendingPathComponent("parent-blocked"))
                        }
                        while true { Thread.sleep(forTimeInterval: 0.02) }
                    }
                } catch {
                    exit(2)
                }
            }

            static func unusedPort() throws -> Int {
                let descriptor = socket(AF_INET, SOCK_STREAM, 0)
                guard descriptor >= 0 else { throw POSIXError(.EIO) }
                defer { close(descriptor) }
                var address = sockaddr_in()
                address.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
                address.sin_family = sa_family_t(AF_INET)
                address.sin_addr = in_addr(s_addr: inet_addr("127.0.0.1"))
                var length = socklen_t(MemoryLayout<sockaddr_in>.size)
                let result = withUnsafeMutablePointer(to: &address) { pointer in
                    pointer.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                        Darwin.bind(descriptor, $0, length) == 0 && getsockname(descriptor, $0, &length) == 0
                    }
                }
                guard result else { throw POSIXError(.EIO) }
                return Int(UInt16(bigEndian: address.sin_port))
            }
        }
        """
}
