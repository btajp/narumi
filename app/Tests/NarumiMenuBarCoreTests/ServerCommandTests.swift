import XCTest

@testable import NarumiMenuBarCore

final class ServerCommandTests: XCTestCase {
    let repo = URL(fileURLWithPath: "/Users/山田 太郎/src/narumi", isDirectory: true)
    let base = ["PATH": "/usr/bin:/bin", "HOME": "/Users/山田 太郎", "NARUMI_RECORDER": "/inherited/recorder"]

    func config(recorder: URL? = nil, dataRoot: String? = nil, repository: URL? = nil) -> ServerConfig {
        ServerConfig(
            repository: repository ?? repo,
            repositorySource: .userDefaults,
            port: 9000,
            serverURL: URL(string: "http://127.0.0.1:9000/mcp")!,
            recorder: recorder,
            logFile: URL(fileURLWithPath: "/Users/山田 太郎/Library/Logs/narumi/server.log"),
            dataRoot: dataRoot)
    }

    func testArgumentsArePositionalParameters() throws {
        let recorder = URL(fileURLWithPath: "/Users/山田 太郎/src/narumi/dist/narumi.app/Contents/MacOS/narumi-recorder")
        let command = try XCTUnwrap(ServerCommand(config: config(recorder: recorder), inheriting: base))

        XCTAssertEqual(command.executable.path, "/bin/zsh")
        XCTAssertEqual(
            command.shellScript,
            "exec uv run --project \"$1\" narumi-server --http --host 127.0.0.1 --port \"$2\""
                + " --recorder \"$3\"")
        // Repo, port and recorder travel as argv after the script ($0 $1 $2 $3): a zsh profile
        // (`~/.zshenv` / `~/.zprofile`) can clobber exported variables, never positional
        // parameters — and paths with spaces / Japanese need no quoting either.
        XCTAssertEqual(
            command.arguments,
            ["-lc", command.shellScript, "narumi-server", "/Users/山田 太郎/src/narumi", "9000", recorder.path])
        // Nothing configuration-specific in the shell text, and no env-var indirection.
        XCTAssertFalse(command.shellScript.contains("/Users"))
        XCTAssertFalse(command.shellScript.contains("9000"))
        XCTAssertFalse(command.shellScript.contains("$NARUMI_"))

        XCTAssertEqual(command.environment["PATH"], "/usr/bin:/bin")  // inherited
        XCTAssertNil(command.environment["NARUMI_HOME"])
        XCTAssertEqual(command.currentDirectory, repo)
    }

    func testRecorderClauseOmittedWhenNil() throws {
        let command = try XCTUnwrap(ServerCommand(config: config(), inheriting: ["PATH": "/bin"]))
        XCTAssertEqual(command.shellScript, ServerCommand.script)
        XCTAssertFalse(command.shellScript.contains("--recorder"))
        XCTAssertEqual(command.arguments, ["-lc", ServerCommand.script, "narumi-server", repo.path, "9000"])

        // An inherited NARUMI_RECORDER is left for the server to honour.
        let inherited = try XCTUnwrap(ServerCommand(config: config(), inheriting: base))
        XCTAssertEqual(inherited.environment["NARUMI_RECORDER"], "/inherited/recorder")
        XCTAssertFalse(inherited.shellScript.contains("--recorder"))
    }

    func testDataRootPassthrough() throws {
        let command = try XCTUnwrap(ServerCommand(config: config(dataRoot: "/tmp/narumi home"), inheriting: base))
        XCTAssertEqual(command.environment["NARUMI_HOME"], "/tmp/narumi home")
        XCTAssertFalse(command.shellScript.contains("narumi home"))
    }

    func testNoRepositoryMeansNoCommand() {
        let unconfigured = ServerConfig(
            repository: nil, repositorySource: nil, port: 8765,
            serverURL: URL(string: "http://127.0.0.1:8765/mcp")!, recorder: nil,
            logFile: URL(fileURLWithPath: "/tmp/server.log"), dataRoot: nil)
        XCTAssertNil(ServerCommand(config: unconfigured, inheriting: base))
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
