import Darwin
import XCTest

@testable import NarumiMenuBarCore

/// Real subprocesses, but no app, uv, Python installation, network or user data. A tiny
/// fixture app uses the production ownership helper around a shell script acting as uv.
final class RuntimeSyncOrphanTests: XCTestCase {
    private struct Timeout: Error {}

    func testCrashedAppCannotClearStagingWhileUVOrDescendantsRemain() throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent("narumi-orphan-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: root) }
        let executable = try compileFixture(in: root)
        for mode in ["uv", "grandchild", "pending", "fast", "server"] {
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
            let record = try? JSONDecoder().decode(RuntimeSyncOwnership.Record.self, from: Data(contentsOf: ownerURL))
            // SIGKILL only this test's fixture app; the guard must never kill its orphan.
            XCTAssertEqual(kill(app.processIdentifier, SIGKILL), 0)
            try wait { !app.isRunning }
            app.waitUntilExit()
            let retry = try launch(executable, arguments: [data.path, "retry"])
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
            }
            if let child = record?.child {
                XCTAssertTrue(try RuntimeSyncOwnership.processGroupExists(child.processGroup), "guard did not stop the orphan")
                try Data().write(to: data.appendingPathComponent("finish"))
                try wait { (try? RuntimeSyncOwnership.processGroupExists(child.processGroup)) == false }
                let afterExit = try launch(executable, arguments: [data.path, "retry"])
                try wait { !afterExit.isRunning }
                afterExit.waitUntilExit()
                XCTAssertEqual(afterExit.terminationStatus, 0, "safe to clear only after the original group exits")
                XCTAssertFalse(FileManager.default.fileExists(atPath: ownerURL.path))
                if mode == "server" {
                    XCTAssertEqual(try Data(contentsOf: activeSentinel), Data("old runtime".utf8))
                    XCTAssertFalse(FileManager.default.fileExists(atPath: paths.transactionJournal.path))
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
        compiler.arguments = ["swiftc", "-swift-version", "6", "-parse-as-library", "-o", executable.path, source.path]
            + ["RuntimeSyncOwnership.swift", "RuntimeGuards.swift", "RuntimeInstallation.swift", "RuntimeSyncPlan.swift", "RuntimeManifest.swift"]
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
        @main struct FakeApp {
            static func main() {
                do {
                    let data = URL(fileURLWithPath: CommandLine.arguments[1])
                    let mode = CommandLine.arguments[2]
                    let paths = RuntimePaths(dataRoot: data)
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
        }
        """
}
