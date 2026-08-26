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

    /// `nil` when the config has no repository (nothing to launch).
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

    /// The shell text (`arguments[1]`).
    public var shellScript: String { arguments[1] }
}
