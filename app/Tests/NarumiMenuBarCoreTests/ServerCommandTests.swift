import Darwin
import XCTest

@testable import NarumiMenuBarCore

final class ServerCommandTests: XCTestCase {
    let repo = URL(fileURLWithPath: "/Users/山田 太郎/src/narumi", isDirectory: true)
    let base = ["PATH": "/usr/bin:/bin", "HOME": "/Users/山田 太郎", "NARUMI_RECORDER": "/inherited/recorder"]
    let dataRootURL = URL(fileURLWithPath: "/Users/山田 太郎/Library/Application Support/narumi", isDirectory: true)

    func config(
        recorder: URL? = nil, dataRoot: String? = nil, repository: URL? = nil,
        runtimeMode: ServerConfig.RuntimeMode? = .repo, bundledRuntime: BundledRuntime? = nil,
        runtimePaths: RuntimePaths? = nil
    ) -> ServerConfig {
        ServerConfig(
            repository: repository ?? repo,
            repositorySource: .userDefaults,
            port: 9000,
            serverURL: URL(string: "http://127.0.0.1:9000/mcp")!,
            recorder: recorder,
            logFile: URL(fileURLWithPath: "/Users/山田 太郎/Library/Logs/narumi/server.log"),
            dataRoot: dataRoot,
            runtimeMode: runtimeMode,
            bundledRuntime: bundledRuntime,
            runtimePaths: runtimePaths ?? RuntimePaths(dataRoot: dataRootURL),
            runtimeLogFile: URL(fileURLWithPath: "/Users/山田 太郎/Library/Logs/narumi/runtime.log"))
    }

    func testArgumentsArePositionalParameters() throws {
        let recorder = URL(fileURLWithPath: "/Users/山田 太郎/src/narumi/dist/narumi.app/Contents/MacOS/narumi-recorder")
        let command = try XCTUnwrap(ServerCommand(config: config(recorder: recorder), inheriting: base))

        XCTAssertEqual(command.executable.path, "/bin/zsh")
        let script = try XCTUnwrap(command.shellScript)
        XCTAssertEqual(
            script,
            "exec uv run --project \"$1\" narumi-server --http --host 127.0.0.1 --port \"$2\""
                + " --recorder \"$3\"")
        // Repo, port and recorder travel as argv after the script ($0 $1 $2 $3): a zsh profile
        // (`~/.zshenv` / `~/.zprofile`) can clobber exported variables, never positional
        // parameters — and paths with spaces / Japanese need no quoting either.
        XCTAssertEqual(
            command.arguments,
            ["-lc", script, "narumi-server", "/Users/山田 太郎/src/narumi", "9000", recorder.path])
        // Nothing configuration-specific in the shell text, and no env-var indirection.
        XCTAssertFalse(script.contains("/Users"))
        XCTAssertFalse(script.contains("9000"))
        XCTAssertFalse(script.contains("$NARUMI_"))

        XCTAssertEqual(command.environment["PATH"], "/usr/bin:/bin")  // inherited
        XCTAssertNil(command.environment["NARUMI_HOME"])
        XCTAssertEqual(command.currentDirectory, repo)
    }

    func testRecorderClauseOmittedWhenNil() throws {
        let command = try XCTUnwrap(ServerCommand(config: config(), inheriting: ["PATH": "/bin"]))
        XCTAssertEqual(command.shellScript, ServerCommand.script)
        XCTAssertFalse(try XCTUnwrap(command.shellScript).contains("--recorder"))
        XCTAssertEqual(command.arguments, ["-lc", ServerCommand.script, "narumi-server", repo.path, "9000"])

        // An inherited NARUMI_RECORDER is left for the server to honour.
        let inherited = try XCTUnwrap(ServerCommand(config: config(), inheriting: base))
        XCTAssertEqual(inherited.environment["NARUMI_RECORDER"], "/inherited/recorder")
        XCTAssertFalse(try XCTUnwrap(inherited.shellScript).contains("--recorder"))
    }

    func testDataRootPassthrough() throws {
        let command = try XCTUnwrap(ServerCommand(config: config(dataRoot: "/tmp/narumi home"), inheriting: base))
        XCTAssertEqual(command.environment["NARUMI_HOME"], "/tmp/narumi home")
        XCTAssertFalse(try XCTUnwrap(command.shellScript).contains("narumi home"))
    }

    func testNoRepositoryMeansNoCommand() {
        let unconfigured = ServerConfig(
            repository: nil, repositorySource: nil, port: 8765,
            serverURL: URL(string: "http://127.0.0.1:8765/mcp")!, recorder: nil,
            logFile: URL(fileURLWithPath: "/tmp/server.log"), dataRoot: nil,
            runtimeMode: nil, bundledRuntime: nil,
            runtimePaths: RuntimePaths(dataRoot: URL(fileURLWithPath: "/tmp/narumi", isDirectory: true)),
            runtimeLogFile: URL(fileURLWithPath: "/tmp/runtime.log"))
        XCTAssertNil(ServerCommand(config: unconfigured, inheriting: base))
    }

    // MARK: Bundled mode

    func testBundledCommandRunsTheVenvPythonInIsolatedMode() throws {
        let runtime = BundledRuntime(
            root: URL(fileURLWithPath: "/Applications/narumi.app/Contents/Resources/runtime", isDirectory: true))
        let recorder = URL(fileURLWithPath: "/Applications/narumi.app/Contents/MacOS/narumi-recorder")
        let command = try XCTUnwrap(
            ServerCommand.bundled(
                config: config(recorder: recorder, runtimeMode: .bundled, bundledRuntime: runtime),
                inheriting: base))

        // The venv binary at an absolute path — no shell, no PATH lookup, no positional tricks.
        XCTAssertEqual(command.executable.path, dataRootURL.path + "/runtime/venv/bin/python3")
        XCTAssertEqual(
            command.arguments,
            [
                "-I", "-m", "narumi_server.cli", "--http", "--host", "127.0.0.1",
                "--port", "9000", "--recorder", recorder.path,
            ])
        XCTAssertNil(command.shellScript)
        // The server reads the contracts copied into the .app.
        XCTAssertEqual(
            command.environment["NARUMI_CONTRACTS_DIR"],
            "/Applications/narumi.app/Contents/Resources/runtime/contracts")
        XCTAssertEqual(command.environment["PATH"], "/usr/bin:/bin")  // inherited
        XCTAssertNil(command.environment["NARUMI_HOME"])  // 既定のまま
        XCTAssertEqual(command.currentDirectory.path, dataRootURL.path + "/runtime")
    }

    func testBundledCommandOmitsRecorderAndPassesDataRootThrough() throws {
        let runtime = BundledRuntime(
            root: URL(fileURLWithPath: "/Applications/narumi.app/Contents/Resources/runtime", isDirectory: true))
        let command = try XCTUnwrap(
            ServerCommand.bundled(
                config: config(dataRoot: "/tmp/narumi home", runtimeMode: .bundled, bundledRuntime: runtime),
                inheriting: base))
        XCTAssertEqual(
            command.arguments,
            ["-I", "-m", "narumi_server.cli", "--http", "--host", "127.0.0.1", "--port", "9000"])
        XCTAssertEqual(command.environment["NARUMI_HOME"], "/tmp/narumi home")
    }

    func testNoBundledRuntimeMeansNoBundledCommand() {
        XCTAssertNil(ServerCommand.bundled(config: config(runtimeMode: .bundled), inheriting: base))
    }

    func testOnlyBundledModeRemovesInheritedPythonOverrides() throws {
        let runtime = BundledRuntime(root: URL(fileURLWithPath: "/Applications/narumi.app/Contents/Resources/runtime"))
        let overrides = [
            "PYTHONPATH": "/old-checkout", "PYTHONHOME": "/other-python", "PYTHONUSERBASE": "/user-packages",
            "PYTHONSTARTUP": "/startup.py", "PYTHONINSPECT": "1", "PYTHONEXECUTABLE": "/other-python",
            "_PYTHON_SYSCONFIGDATA_NAME": "custom_config", "__PYVENV_LAUNCHER__": "/other-python",
        ]
        var inherited = base.merging(overrides) { _, new in new }
        inherited["HTTPS_PROXY"] = "http://127.0.0.1:8080"
        inherited["ANTHROPIC_API_KEY"] = "fake-test-credential"
        let bundled = try XCTUnwrap(
            ServerCommand.bundled(
                config: config(runtimeMode: .bundled, bundledRuntime: runtime), inheriting: inherited))
        for key in overrides.keys {
            XCTAssertNil(bundled.environment[key], key)
        }
        for key in ["PATH", "HOME", "HTTPS_PROXY", "ANTHROPIC_API_KEY"] {
            XCTAssertEqual(bundled.environment[key], inherited[key], key)
        }
        let development = try XCTUnwrap(ServerCommand(config: config(), inheriting: inherited))
        XCTAssertEqual(development.environment, inherited, "Explicit development mode keeps its Python configuration")
    }

    func testBundledPythonIgnoresSameVersionCheckoutAndWorkingDirectoryPackages() throws {
        try withFakePythonRuntime { root, paths, environment in
            let checkout = root.appendingPathComponent("old checkout", isDirectory: true)
            try writeFakeServer(in: checkout, origin: "old-checkout")
            try writeFakeServer(in: paths.root, origin: "working-directory")
            let python = paths.venv.appendingPathComponent("bin/python3")
            var inherited = environment
            inherited["PYTHONPATH"] = checkout.path

            // Both stale packages claim the bundled version: version-based readiness cannot
            // distinguish them. These controls prove the fixture really shadows the venv.
            let old = try pythonReport(
                executable: python, arguments: ["-m", "narumi_server.cli"], directory: root, environment: inherited)
            XCTAssertEqual(old["origin"] as? String, "old-checkout")
            let cwd = try pythonReport(
                executable: python, arguments: ["-m", "narumi_server.cli"], directory: paths.root, environment: environment)
            XCTAssertEqual(cwd["origin"] as? String, "working-directory")

            // A bogus home/launcher would also break Python before readiness without isolation.
            inherited["PYTHONHOME"] = root.appendingPathComponent("nonexistent-python").path
            inherited["__PYVENV_LAUNCHER__"] = root.appendingPathComponent("nonexistent-launcher").path
            let command = try fakeBundledCommand(root: root, paths: paths, environment: inherited)
            let isolated = try pythonReport(command)
            XCTAssertEqual(isolated["origin"] as? String, "bundled")
            XCTAssertEqual(isolated["version"] as? String, "0.1.1")
            XCTAssertEqual(isolated["version"] as? String, old["version"] as? String)
            XCTAssertEqual(isolated["version"] as? String, cwd["version"] as? String)
            XCTAssertEqual(isolated["isolated"] as? Int, 1)
            XCTAssertEqual(isolated["ignore_environment"] as? Int, 1)
        }
    }

    func testBundledPythonDisablesDefaultUserSite() throws {
        // Deliberately enable user sites in this fixture's venv. The release venv disables
        // them too, but the actual launch command must independently enforce that boundary.
        try withFakePythonRuntime(systemSitePackages: true) { root, paths, environment in
            let python = paths.venv.appendingPathComponent("bin/python3")
            let sitePath = try pythonOutput(
                executable: python, arguments: ["-c", "import site; print(site.getusersitepackages())"],
                directory: root, environment: environment).trimmingCharacters(in: .whitespacesAndNewlines)
            let userSite = URL(fileURLWithPath: sitePath, isDirectory: true)
            try requireFixturePath(userSite, under: root)
            try FileManager.default.createDirectory(at: userSite, withIntermediateDirectories: true)
            try Data("__version__ = '0.1.1'\n".utf8).write(to: userSite.appendingPathComponent("narumi_legacy_user_site.py"))

            let unisolated = try pythonReport(
                executable: python, arguments: ["-m", "narumi_server.cli"], directory: root, environment: environment)
            XCTAssertEqual(unisolated["user_site_enabled"] as? Bool, true)
            XCTAssertEqual(unisolated["user_site_module"] as? Bool, true)
            let isolated = try pythonReport(fakeBundledCommand(root: root, paths: paths, environment: environment))
            XCTAssertEqual(isolated["origin"] as? String, "bundled")
            XCTAssertEqual(isolated["user_site_enabled"] as? Bool, false)
            XCTAssertEqual(isolated["user_site_module"] as? Bool, false)
            XCTAssertEqual(isolated["isolated"] as? Int, 1)
        }
    }

    // No real narumi imports, uv, downloads, services, or user directories are involved.
    private func withFakePythonRuntime(
        systemSitePackages: Bool = false, _ body: (URL, RuntimePaths, [String: String]) throws -> Void
    ) throws {
        let fm = FileManager.default
        let systemPython = URL(fileURLWithPath: "/usr/bin/python3")
        guard fm.isExecutableFile(atPath: systemPython.path) else {
            throw XCTSkip("The fake Python runtime test requires the macOS developer-tools Python")
        }
        let root = fm.temporaryDirectory.appendingPathComponent("narumi-python-\(UUID().uuidString)", isDirectory: true)
        try fm.createDirectory(at: root, withIntermediateDirectories: true)
        defer { try? fm.removeItem(at: root) }
        let environment = ["PATH": "/usr/bin:/bin", "HOME": root.path]
        let paths = RuntimePaths(dataRoot: root)
        var arguments = ["-I", "-m", "venv", "--without-pip"]
        if systemSitePackages {
            arguments.append("--system-site-packages")
        }
        arguments.append(paths.venv.path)
        _ = try pythonOutput(executable: systemPython, arguments: arguments, directory: root, environment: environment)
        let python = paths.venv.appendingPathComponent("bin/python3")
        let sitePath = try pythonOutput(
            executable: python, arguments: ["-I", "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],
            directory: root, environment: environment).trimmingCharacters(in: .whitespacesAndNewlines)
        let site = URL(fileURLWithPath: sitePath, isDirectory: true)
        try requireFixturePath(site, under: root)
        try writeFakeServer(in: site, origin: "bundled")
        try body(root, paths, environment)
    }

    private func requireFixturePath(_ path: URL, under root: URL) throws {
        guard path.resolvingSymlinksInPath().path.hasPrefix(root.resolvingSymlinksInPath().path + "/") else {
            throw NSError(
                domain: "FakePythonRuntime", code: 1,
                userInfo: [NSLocalizedDescriptionKey: "Unsafe fixture path"])
        }
    }

    private func fakeBundledCommand(root: URL, paths: RuntimePaths, environment: [String: String]) throws -> ServerCommand {
        let runtime = BundledRuntime(root: root.appendingPathComponent("bundle/runtime", isDirectory: true))
        return try XCTUnwrap(
            ServerCommand.bundled(
                config: config(dataRoot: root.path, runtimeMode: .bundled, bundledRuntime: runtime, runtimePaths: paths),
                inheriting: environment))
    }

    private func writeFakeServer(in directory: URL, origin: String) throws {
        let package = directory.appendingPathComponent("narumi_server", isDirectory: true)
        try FileManager.default.createDirectory(at: package, withIntermediateDirectories: true)
        try Data("__version__ = '0.1.1'\nORIGIN = '\(origin)'\n".utf8)
            .write(to: package.appendingPathComponent("__init__.py"))
        try Data(
            """
            import importlib.util, json, site, sys
            from narumi_server import ORIGIN, __version__
            print(json.dumps({
                "origin": ORIGIN, "version": __version__, "isolated": sys.flags.isolated,
                "ignore_environment": sys.flags.ignore_environment,
                "user_site_enabled": site.ENABLE_USER_SITE is True,
                "user_site_module": importlib.util.find_spec("narumi_legacy_user_site") is not None,
            }))
            """.utf8
        ).write(to: package.appendingPathComponent("cli.py"))
    }

    private func pythonReport(_ command: ServerCommand) throws -> [String: Any] {
        try pythonReport(
            executable: command.executable, arguments: command.arguments,
            directory: command.currentDirectory, environment: command.environment)
    }

    private func pythonReport(
        executable: URL, arguments: [String], directory: URL, environment: [String: String]
    ) throws -> [String: Any] {
        let output = try pythonOutput(
            executable: executable, arguments: arguments, directory: directory, environment: environment)
        return try XCTUnwrap(JSONSerialization.jsonObject(with: Data(output.utf8)) as? [String: Any])
    }

    private func pythonOutput(
        executable: URL, arguments: [String], directory: URL, environment: [String: String]
    ) throws -> String {
        let process = Process()
        process.executableURL = executable
        process.arguments = arguments
        process.currentDirectoryURL = directory
        process.environment = environment
        let output = Pipe()
        process.standardOutput = output
        process.standardError = output
        let finished = expectation(description: "fake Python process finished")
        process.terminationHandler = { _ in finished.fulfill() }
        try process.run()
        guard XCTWaiter.wait(for: [finished], timeout: 20) == .completed else {
            if process.isRunning {
                kill(process.processIdentifier, SIGKILL)
            }
            throw NSError(
                domain: "FakePythonRuntime", code: 2,
                userInfo: [NSLocalizedDescriptionKey: "Python fixture timed out"])
        }
        let result = String(decoding: output.fileHandleForReading.readDataToEndOfFile(), as: UTF8.self)
        guard process.terminationStatus == 0 else {
            throw NSError(
                domain: "FakePythonRuntime", code: Int(process.terminationStatus),
                userInfo: [NSLocalizedDescriptionKey: result])
        }
        return result
    }

    /// Regression test for the profile-clobbering bug: run the *actual* command through
    /// `/bin/zsh -l` with a `.zprofile` that exports conflicting `NARUMI_*` values and a fake
    /// `uv` that echoes its argv. The launched server must still get the app's repo, port and
    /// recorder. No network, no real uv: everything lives in a temp directory.
    func testLoginShellProfileCannotOverrideParameters() throws {
        let fm = FileManager.default
        let root = fm.temporaryDirectory.appendingPathComponent("narumi-cmd-\(UUID().uuidString)")
        defer { try? fm.removeItem(at: root) }
        let repoDir = root.appendingPathComponent("リポ ジトリ", isDirectory: true)
        let binDir = root.appendingPathComponent("bin", isDirectory: true)
        let zdotDir = root.appendingPathComponent("zdot", isDirectory: true)
        for dir in [repoDir, binDir, zdotDir] {
            try fm.createDirectory(at: dir, withIntermediateDirectories: true)
        }
        let fakeUV = binDir.appendingPathComponent("uv")
        try Data("#!/bin/sh\nprintf '%s\\n' \"$@\"\n".utf8).write(to: fakeUV)
        try fm.setAttributes([.posixPermissions: 0o755], ofItemAtPath: fakeUV.path)
        // Sourced by `zsh -l` after the app-provided environment: tries to clobber everything,
        // and puts the fake uv first on PATH (after /etc/zprofile's path_helper ran).
        try Data(
            """
            export PATH="\(binDir.path):$PATH"
            export NARUMI_REPO=/from-profile
            export NARUMI_SERVER_PORT=1
            export NARUMI_RECORDER=/from-profile/recorder
            """.utf8
        ).write(to: zdotDir.appendingPathComponent(".zprofile"))

        let recorder = repoDir.appendingPathComponent("narumi-recorder")
        let command = try XCTUnwrap(
            ServerCommand(
                config: config(recorder: recorder, repository: repoDir),
                inheriting: ["PATH": "/usr/bin:/bin", "ZDOTDIR": zdotDir.path]))

        let process = Process()
        process.executableURL = command.executable
        process.arguments = command.arguments
        process.currentDirectoryURL = command.currentDirectory
        process.environment = command.environment
        let stdout = Pipe()
        process.standardOutput = stdout
        process.standardError = FileHandle.nullDevice
        try process.run()
        let data = stdout.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()

        XCTAssertEqual(process.terminationStatus, 0)
        let argv = String(decoding: data, as: UTF8.self).split(separator: "\n").map(String.init)
        XCTAssertEqual(
            argv,
            [
                "run", "--project", repoDir.path, "narumi-server", "--http",
                "--host", "127.0.0.1", "--port", "9000", "--recorder", recorder.path,
            ])
    }
}
