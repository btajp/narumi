import Foundation

/// Layout of `<narumi.app>/Contents/Resources/runtime/` — the bootstrap payload the .app
/// carries so it can build its own venv (spec `2026-08-27-narumi-app-distribution-design.md` §1).
public struct BundledRuntime: Equatable, Sendable {
    /// Relative to the .app bundle URL.
    public static let bundleSubpath = "Contents/Resources/runtime"

    /// `<narumi.app>/Contents/Resources/runtime`.
    public var root: URL

    public init(root: URL) {
        self.root = root
    }

    /// Standalone `uv` binary (version pinned by the release build, sha256-verified there).
    public var uv: URL { root.appendingPathComponent("uv") }
    public var manifest: URL { root.appendingPathComponent("manifest.json") }
    /// Fully pinned third-party requirements (`uv export`, with hashes).
    public var requirements: URL { root.appendingPathComponent("requirements.txt") }
    public var wheelsDir: URL { root.appendingPathComponent("wheels", isDirectory: true) }
    /// Copy of the repo's `contracts/`; handed to the server as `NARUMI_CONTRACTS_DIR`.
    public var contractsDir: URL { root.appendingPathComponent("contracts", isDirectory: true) }

    public func wheel(_ name: String) -> URL {
        wheelsDir.appendingPathComponent(name)
    }
}

/// Where the synced runtime lives under the effective data root (`NARUMI_HOME`, default
/// `~/Library/Application Support/narumi`): `runtime/{python, venv, venv.new, installed.json}`.
/// Files under here carry no quarantine attribute, so Gatekeeper never sees them.
public struct RuntimePaths: Equatable, Sendable {
    /// `<data root>/runtime`.
    public var root: URL

    public init(dataRoot: URL) {
        root = dataRoot.appendingPathComponent("runtime", isDirectory: true)
    }

    /// `UV_PYTHON_INSTALL_DIR` — uv-managed Python installs.
    public var pythonDir: URL { root.appendingPathComponent("python", isDirectory: true) }
    public var venv: URL { root.appendingPathComponent("venv", isDirectory: true) }
    /// Staging venv: fully built here, then swapped into `venv`, so a failed sync (no
    /// network, bad wheel) never destroys a previously working venv.
    public var venvStaging: URL { root.appendingPathComponent("venv.new", isDirectory: true) }
    /// Copy of the bundle manifest written after a successful sync.
    public var installedManifest: URL { root.appendingPathComponent("installed.json") }
    /// The bundled-mode server entry point.
    public var serverExecutable: URL { venv.appendingPathComponent("bin/narumi-server") }
}

/// The ordered steps that (re)build the bundled venv (spec §1 手順 1–4). Pure data: the
/// launcher executes the steps, tests inspect the argv without running uv.
///
/// Every uv step targets `venv.new`; the swap into `venv` and the `installed.json` write come
/// last, so any failure leaves the old venv (and the "needs sync" marker) intact.
public struct RuntimeSyncPlan: Equatable, Sendable {
    /// A subprocess to run. `environment` holds only the *additions* the executor merges over
    /// the app's environment (uv still needs HOME / PATH-adjacent variables from it).
    public struct Command: Equatable, Sendable {
        public var executable: URL
        public var arguments: [String]
        public var environment: [String: String]

        public init(executable: URL, arguments: [String], environment: [String: String]) {
            self.executable = executable
            self.arguments = arguments
            self.environment = environment
        }
    }

    public enum Step: Equatable, Sendable {
        /// Run a subprocess (uv), output appended to runtime.log.
        case run(name: String, Command)
        /// Remove `to` when present, then rename `from` → `to` (venv.new → venv).
        case replaceDirectory(name: String, from: URL, to: URL)
        /// Copy the bundle's manifest.json bytes to installed.json (marks the sync done).
        case copyFile(name: String, from: URL, to: URL)

        /// Progress label: menu shows 「サーバー: 環境を準備中…（<name>）」.
        public var name: String {
            switch self {
            case .run(let name, _), .replaceDirectory(let name, _, _), .copyFile(let name, _, _):
                return name
            }
        }
    }

    public var steps: [Step]

    public init(bundle: BundledRuntime, paths: RuntimePaths, manifest: RuntimeManifest) {
        // UV_PYTHON_INSTALL_DIR on every uv step: step 1 installs Python there, and `uv venv
        // --python <ver>` must find that same managed install instead of downloading another
        // copy to uv's default location.
        let uvEnvironment = ["UV_PYTHON_INSTALL_DIR": paths.pythonDir.path]
        func uvStep(_ name: String, _ arguments: [String]) -> Step {
            .run(
                name: name,
                Command(executable: bundle.uv, arguments: arguments, environment: uvEnvironment))
        }
        var steps: [Step] = [
            uvStep("Python 取得", ["python", "install", manifest.python]),
            // --relocatable: entry-point scripts locate python relative to themselves instead
            // of hardcoding the venv path — required because the venv is built at venv.new and
            // then renamed to venv (without it, venv/bin/narumi-server keeps a dead
            // `#!…/venv.new/bin/python3` shebang and exits 126; verified with the real uv).
            uvStep(
                "venv 作成",
                ["venv", paths.venvStaging.path, "--clear", "--relocatable", "--python", manifest.python]),
            uvStep(
                "依存インストール",
                [
                    "pip", "install", "--python", paths.venvStaging.path,
                    "--require-hashes", "-r", bundle.requirements.path,
                ]),
        ]
        // Sorted for a deterministic argv (the manifest dictionary has no order).
        let wheelPaths = manifest.wheels.keys.sorted().map { bundle.wheel($0).path }
        if !wheelPaths.isEmpty {
            steps.append(
                uvStep(
                    "アプリ本体インストール",
                    ["pip", "install", "--python", paths.venvStaging.path, "--no-deps"] + wheelPaths))
        }
        steps.append(.replaceDirectory(name: "venv 差し替え", from: paths.venvStaging, to: paths.venv))
        steps.append(.copyFile(name: "同期の記録", from: bundle.manifest, to: paths.installedManifest))
        self.steps = steps
    }
}
