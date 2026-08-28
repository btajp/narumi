import Foundation

/// The `Process` launch spec for `narumi-server`, derived from a `ServerConfig`.
///
/// The shell text is a fixed string that only *references* positional parameters (`$1` = repo,
/// `$2` = port, `$3` = recorder); repository, port and recorder travel as argv after the script
/// and are never interpolated into it, so paths with spaces or Japanese characters need no
/// quoting. Positional parameters — unlike environment variables — cannot be clobbered by the
/// login shell's profiles: `~/.zshenv` / `~/.zprofile` run *after* the app-provided environment
/// is inherited, so a developer's `export NARUMI_REPO=…` there would silently override an
/// env-var indirection (wrong checkout launched, or a port the app never polls). `zsh -l` runs
/// the login profile, which puts `uv` on PATH (`~/.local/bin`, `/opt/homebrew/bin`, nix
/// profiles) — a GUI app inherits only launchd's minimal PATH. `exec` makes the shell *become*
/// uv, so the pid the app holds is the one to signal (uv forwards SIGTERM to the Python server).
public struct ServerCommand: Equatable, Sendable {
    public static let shell = URL(fileURLWithPath: "/bin/zsh")
    public static let script =
        "exec uv run --project \"$1\" narumi-server --http --host 127.0.0.1 --port \"$2\""
    public static let recorderClause = " --recorder \"$3\""
    /// `$0` of the shell script; shows up in `ps` while the shell is alive.
    public static let scriptName = "narumi-server"

    public let executable: URL
    public let arguments: [String]
    public let currentDirectory: URL
    public let environment: [String: String]

    init(executable: URL, arguments: [String], currentDirectory: URL, environment: [String: String]) {
        self.executable = executable
        self.arguments = arguments
        self.currentDirectory = currentDirectory
        self.environment = environment
    }

    /// Repo mode. `nil` when the config has no repository (nothing to launch).
    public init?(config: ServerConfig, inheriting base: [String: String] = ProcessInfo.processInfo.environment) {
        guard let repository = config.repository else {
            return nil
        }
        var environment = base
        var script = Self.script
        var parameters = [repository.path, String(config.port)]
        if let recorder = config.recorder {
            script += Self.recorderClause
            parameters.append(recorder.path)
        }
        // Without a bundled recorder `--recorder` is omitted: the server then honours an
        // inherited NARUMI_RECORDER or falls back to app/.build/{release,debug}/narumi-recorder.
        if let dataRoot = config.dataRoot {
            environment[ServerConfig.Env.home] = dataRoot
        }
        executable = Self.shell
        arguments = ["-lc", script, Self.scriptName] + parameters
        currentDirectory = repository
        self.environment = environment
    }

    /// Bundled mode (spec `2026-08-27-narumi-app-distribution-design.md` §1): run the synced
    /// venv's Python in isolated mode — no login shell, PATH lookup, user site, working-directory
    /// import, or inherited Python overrides. Otherwise an old checkout on PYTHONPATH could
    /// pass readiness checks with the same version as the bundled package. The CLI module is
    /// the same entry point as `narumi-server`. `NARUMI_CONTRACTS_DIR` points at the contracts
    /// copied into the .app; `NARUMI_HOME` passes through exactly like repo mode. `nil` when
    /// the .app carries no `Resources/runtime`.
    public static func bundled(
        config: ServerConfig, inheriting base: [String: String] = ProcessInfo.processInfo.environment
    ) -> ServerCommand? {
        guard let runtime = config.bundledRuntime else {
            return nil
        }
        // `-I` protects this interpreter; remove import/startup overrides as well so Python
        // subprocesses cannot reintroduce them. Keep unrelated settings (auth, proxy, locale,
        // etc.). macOS's launcher override is consumed before normal Python flag handling.
        var environment = base.filter { key, _ in
            !key.hasPrefix("PYTHON") && !key.hasPrefix("_PYTHON") && key != "__PYVENV_LAUNCHER__"
        }
        environment[ServerConfig.Env.contractsDir] = runtime.contractsDir.path
        if let dataRoot = config.dataRoot {
            environment[ServerConfig.Env.home] = dataRoot
        }
        var arguments = [
            "-I", "-m", "narumi_server.cli",
            "--http", "--host", ServerConfig.defaultHost, "--port", String(config.port),
        ]
        if let recorder = config.recorder {
            arguments += ["--recorder", recorder.path]
        }
        return ServerCommand(
            executable: config.runtimePaths.venv.appendingPathComponent("bin/python3"),
            arguments: arguments,
            // Exists whenever the launch is reached: the venv (inside it) was just synced.
            currentDirectory: config.runtimePaths.root,
            environment: environment)
    }

    /// The shell text (`arguments[1]`) of a repo-mode command; `nil` for the bundled command,
    /// which runs the venv binary directly without a shell.
    public var shellScript: String? {
        executable == Self.shell ? arguments[1] : nil
    }
}
