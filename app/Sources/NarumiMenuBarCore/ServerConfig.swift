import Foundation

/// Where narumi.app finds the narumi repository and how it reaches / launches `narumi-server`.
///
/// A pure value. `resolve` takes every environmental input as a parameter (environment, the
/// stored `UserDefaults` value, the bundle location, a file-existence probe) so tests never
/// touch `UserDefaults`, `Bundle.main` or the file system.
public struct ServerConfig: Equatable, Sendable {
    /// Environment variables read here and passed on to the server process.
    public enum Env {
        /// Repository checkout (highest precedence for the repository).
        public static let repo = "NARUMI_REPO"
        /// HTTPS port the launched server binds; also derives the MCP URL.
        public static let port = "NARUMI_SERVER_PORT"
        /// Explicit pinned HTTPS MCP URL; never an alternate HTTP endpoint.
        public static let serverURL = "NARUMI_SERVER_URL"
        /// Data root; passed through to the server when set.
        public static let home = "NARUMI_HOME"
        /// Recorder binary; handed to the server as `--recorder`.
        public static let recorder = "NARUMI_RECORDER"
        public static let keychainHelper = "NARUMI_KEYCHAIN_HELPER"
        /// Forces `repo` or `bundled` runtime mode (dev / E2E override; any other value is
        /// ignored and the automatic detection applies).
        public static let runtimeMode = "NARUMI_RUNTIME_MODE"
        /// Contracts directory the server reads; the app sets it in bundled mode to the copy
        /// inside `Resources/runtime/contracts`.
        public static let contractsDir = "NARUMI_CONTRACTS_DIR"
        /// Sparkle appcast override for the updater E2E; never set in production. Read by
        /// `AppDelegate.feedURLString(for:)`.
        public static let sparkleFeedURL = "NARUMI_SPARKLE_FEED_URL"
    }

    /// How `narumi-server` is run (spec `2026-08-27-narumi-app-distribution-design.md` §1).
    ///
    /// Precedence: `NARUMI_RUNTIME_MODE` → explicit `NARUMI_REPO` → bundled resources →
    /// saved / inferred repository → not configured. A release must not silently run an old
    /// checkout just because a development app previously saved its path. Explicit developer
    /// overrides still win, even if their inputs are missing (a visible startup failure).
    public enum RuntimeMode: String, Equatable, Sendable {
        case repo
        case bundled
    }

    public static let defaultPort = 8765
    public static let defaultHost = "127.0.0.1"
    public static let mcpPath = "/mcp"
    /// `UserDefaults` key holding the repository chosen with 「リポジトリを選択…」.
    public static let repoPathDefaultsKey = "narumi.repoPath"
    /// Files that make a directory the narumi repository (uv workspace root + `server` member).
    public static let repositoryMarkers = ["pyproject.toml", "server/pyproject.toml"]
    public static let recorderPathInBundle = "Contents/MacOS/narumi-recorder"
    public static let keychainHelperPathInBundle = "Contents/MacOS/narumi-keychain"
    public static let logPathInHome = "Library/Logs/narumi/server.log"
    /// Output of the bundled-runtime sync (uv), separate from the server log.
    public static let runtimeLogPathInHome = "Library/Logs/narumi/runtime.log"
    /// The server's default data root when `NARUMI_HOME` is unset
    /// (`narumi.config.data_root`); the bundled venv lives under `<data root>/runtime`.
    public static let dataRootPathInHome = "Library/Application Support/narumi"
    static let validPorts = 1...65535

    public enum RepositorySource: Equatable, Sendable {
        case environment
        case userDefaults
        /// `<repo>/dist/narumi.app` — the layout `scripts/build-app.sh` produces.
        case bundle
    }

    /// Repository checkout to run `uv run narumi-server` in; `nil` = not configured.
    public var repository: URL?
    public var repositorySource: RepositorySource?
    /// Port the launched server binds (`NARUMI_SERVER_PORT`, else the port of
    /// `NARUMI_SERVER_URL`, else 8765).
    public var port: Int
    /// MCP endpoint the client talks to (`NARUMI_SERVER_URL`, else derived from `port`).
    public var serverURL: URL
    /// Whether the user explicitly selected a server endpoint. A bundled release may only
    /// attach to an already-running, unowned server when this is true.
    public var hasExplicitServerURL: Bool
    /// `narumi-recorder` shipped inside the .app, when present.
    public var recorder: URL?
    /// The same signed helper used by the resident server owns its Keychain entries.
    public var keychainHelper: URL?
    public var keychainHelperLocations: [KeychainHelperLocation]
    public var rejectedKeychainHelperOverride: Bool
    /// stdout + stderr of the launched server.
    public var logFile: URL
    /// `NARUMI_HOME` passthrough (nil = the server's default data root).
    public var dataRoot: String?
    /// How to run the server; `nil` = not configured (no repository, no bundled runtime).
    public var runtimeMode: RuntimeMode?
    /// `Resources/runtime` inside the .app, when present.
    public var bundledRuntime: BundledRuntime?
    /// venv / python / installed.json under the effective data root.
    public var runtimePaths: RuntimePaths
    /// stdout + stderr of the bundled-runtime sync (`~/Library/Logs/narumi/runtime.log`).
    public var runtimeLogFile: URL

    public init(
        repository: URL?,
        repositorySource: RepositorySource?,
        port: Int,
        serverURL: URL,
        recorder: URL?,
        logFile: URL,
        dataRoot: String?,
        runtimeMode: RuntimeMode?,
        bundledRuntime: BundledRuntime?,
        runtimePaths: RuntimePaths,
        runtimeLogFile: URL,
        hasExplicitServerURL: Bool = false,
        keychainHelper: URL? = nil,
        keychainHelperLocations: [KeychainHelperLocation] = [],
        rejectedKeychainHelperOverride: Bool = false
    ) {
        self.repository = repository
        self.repositorySource = repositorySource
        self.port = port
        self.serverURL = serverURL
        self.hasExplicitServerURL = hasExplicitServerURL
        self.recorder = recorder
        self.keychainHelper = keychainHelper
        self.keychainHelperLocations = keychainHelperLocations
        self.rejectedKeychainHelperOverride = rejectedKeychainHelperOverride
        self.logFile = logFile
        self.dataRoot = dataRoot
        self.runtimeMode = runtimeMode
        self.bundledRuntime = bundledRuntime
        self.runtimePaths = runtimePaths
        self.runtimeLogFile = runtimeLogFile
    }

    // MARK: Resolution

    /// Resolve from the environment. Repository precedence: `NARUMI_REPO` → `storedRepoPath`
    /// (the `narumi.repoPath` default) → the bundle heuristic (the .app lives at
    /// `<repo>/dist/narumi.app` and `<repo>` holds both repository markers) → nil.
    public static func resolve(
        environment: [String: String] = ProcessInfo.processInfo.environment,
        storedRepoPath: String?,
        bundleURL: URL? = Bundle.main.bundleURL,
        homeDirectory: URL = FileManager.default.homeDirectoryForCurrentUser,
        fileExists: (URL) -> Bool = { FileManager.default.fileExists(atPath: $0.path) },
        canonicalize: (URL) -> URL = { $0.standardizedFileURL.resolvingSymlinksInPath() }
    ) -> ServerConfig {
        let (repository, source) = resolveRepository(
            environment: environment, storedRepoPath: storedRepoPath, bundleURL: bundleURL,
            fileExists: fileExists)
        let port = resolvePort(environment: environment)
        var recorder: URL?
        var bundledRuntime: BundledRuntime?
        if let bundleURL {
            let candidate = bundleURL.appendingPathComponent(recorderPathInBundle)
            if fileExists(candidate) {
                recorder = candidate
            }
            let runtimeRoot = bundleURL.appendingPathComponent(
                BundledRuntime.bundleSubpath, isDirectory: true)
            if fileExists(runtimeRoot) {
                bundledRuntime = BundledRuntime(root: runtimeRoot)
            }
        }
        let dataRoot = nonEmpty(environment[Env.home])
        let dataRootURL =
            dataRoot.map(directoryURL)
            ?? homeDirectory.appendingPathComponent(dataRootPathInHome, isDirectory: true)
        let mode = resolveRuntimeMode(environment: environment, repository: repository, bundledRuntime: bundledRuntime)
        let helperLocations = Self.helperLocations(mode: mode, repository: repository, bundleURL: bundleURL)
        let helper = resolveKeychainHelper(
            environment: environment, locations: helperLocations, fileExists: fileExists, canonicalize: canonicalize)
        return ServerConfig(
            repository: repository,
            repositorySource: source,
            port: port,
            serverURL: resolveServerURL(environment: environment, port: port),
            recorder: recorder,
            logFile: homeDirectory.appendingPathComponent(logPathInHome),
            dataRoot: dataRoot,
            runtimeMode: mode,
            bundledRuntime: bundledRuntime,
            runtimePaths: RuntimePaths(dataRoot: dataRootURL),
            runtimeLogFile: homeDirectory.appendingPathComponent(runtimeLogPathInHome),
            hasExplicitServerURL: nonEmpty(environment[Env.serverURL]) != nil,
            keychainHelper: helper.url, keychainHelperLocations: helperLocations,
            rejectedKeychainHelperOverride: helper.rejected)
    }

    public var bootstrapDataRoot: URL { runtimePaths.root.deletingLastPathComponent() }

    public func validateSecureEndpoint() throws {
        let endpoint = try MCPServerEndpoint.validate(serverURL)
        guard endpoint.port == port else { throw MCPConnectionError.endpointMismatch }
        guard !rejectedKeychainHelperOverride else { throw MCPConnectionError.credentialUnavailable }
    }

    public func validatedKeychainHelper() throws -> URL {
        guard !rejectedKeychainHelperOverride, let keychainHelper,
            let location = keychainHelperLocations.first(where: { $0.candidate == keychainHelper })
        else { throw MCPConnectionError.credentialUnavailable }
        return try location.validatedExecutable()
    }

    /// An implicitly configured bundled app must own the server it uses. In particular,
    /// finding a development server on the default port is a conflict, not readiness.
    public var requiresOwnedServer: Bool {
        runtimeMode == .bundled && !hasExplicitServerURL
    }

    /// Whether `url` looks like the narumi repository (both markers present).
    public static func isRepository(
        _ url: URL, fileExists: (URL) -> Bool = { FileManager.default.fileExists(atPath: $0.path) }
    ) -> Bool {
        repositoryMarkers.allSatisfy { fileExists(url.appendingPathComponent($0)) }
    }

    static func resolveRepository(
        environment: [String: String], storedRepoPath: String?, bundleURL: URL?,
        fileExists: (URL) -> Bool
    ) -> (URL?, RepositorySource?) {
        if let raw = nonEmpty(environment[Env.repo]) {
            return (directoryURL(raw), .environment)
        }
        if let raw = nonEmpty(storedRepoPath) {
            return (directoryURL(raw), .userDefaults)
        }
        if let bundleURL {
            // <repo>/dist/narumi.app → <repo>
            let candidate = bundleURL.deletingLastPathComponent().deletingLastPathComponent().standardizedFileURL
            if isRepository(candidate, fileExists: fileExists) {
                return (candidate, .bundle)
            }
        }
        return (nil, nil)
    }

    /// See `RuntimeMode`: explicit developer overrides → bundled → saved / inferred repo.
    static func resolveRuntimeMode(
        environment: [String: String], repository: URL?, bundledRuntime: BundledRuntime?
    ) -> RuntimeMode? {
        if let raw = nonEmpty(environment[Env.runtimeMode]), let forced = RuntimeMode(rawValue: raw) {
            return forced
        }
        if nonEmpty(environment[Env.repo]) != nil {
            return .repo
        }
        if bundledRuntime != nil {
            return .bundled
        }
        return repository == nil ? nil : .repo
    }

    static func resolvePort(environment: [String: String]) -> Int {
        if let raw = nonEmpty(environment[Env.port]) {
            if let value = Int(raw), validPorts.contains(value) {
                return value
            }
            return defaultPort  // unparsable: fall back rather than launch on a surprise port
        }
        if let url = explicitServerURL(environment), let value = url.port, validPorts.contains(value) {
            return value
        }
        return defaultPort
    }

    static func resolveServerURL(environment: [String: String], port: Int) -> URL {
        if let raw = nonEmpty(environment[Env.serverURL]) {
            // Preserve an invalid explicit choice as a visible error, never a fallback.
            return URL(string: raw) ?? URL(string: "invalid:")!
        }
        return URL(string: "https://\(defaultHost):\(port)\(mcpPath)")!
    }

    static func resolveKeychainHelper(
        environment: [String: String], locations: [KeychainHelperLocation], fileExists: (URL) -> Bool,
        canonicalize: (URL) -> URL
    ) -> (url: URL?, rejected: Bool) {
        if let explicit = nonEmpty(environment[Env.keychainHelper]) {
            let path = (explicit as NSString).expandingTildeInPath
            guard (path as NSString).isAbsolutePath else { return (nil, true) }
            let requested = URL(fileURLWithPath: path).standardizedFileURL
            guard let known = locations.first(where: { canonicalize($0.candidate) == canonicalize(requested) }) else {
                return (nil, true)
            }
            return (known.candidate, false)
        }
        return (locations.first(where: { fileExists($0.candidate) })?.candidate, false)
    }

    static func helperLocations(mode: RuntimeMode?, repository: URL?, bundleURL: URL?) -> [KeychainHelperLocation] {
        if mode == .bundled, let bundleURL, bundleURL.pathExtension == "app" {
            return [KeychainHelperLocation(trustedRoot: bundleURL,
                candidate: bundleURL.appendingPathComponent(keychainHelperPathInBundle))]
        }
        if mode == .repo, let repository {
            let build = repository.appendingPathComponent("app/.build")
            return [KeychainHelperLocation(trustedRoot: repository,
                candidate: repository.appendingPathComponent("dist/narumi.app/\(keychainHelperPathInBundle)"))]
                + ["release", "debug"].map {
                    KeychainHelperLocation(trustedRoot: repository,
                        candidate: build.appendingPathComponent("\($0)/narumi-keychain"), buildRoot: build)
                }
        }
        return []
    }

    static func explicitServerURL(_ environment: [String: String]) -> URL? {
        guard let raw = nonEmpty(environment[Env.serverURL]), let url = URL(string: raw),
            url.scheme != nil, url.host != nil
        else {
            return nil
        }
        return url
    }

    static func directoryURL(_ path: String) -> URL {
        URL(fileURLWithPath: (path as NSString).expandingTildeInPath, isDirectory: true).standardizedFileURL
    }

    static func nonEmpty(_ value: String?) -> String? {
        guard let value, !value.isEmpty else {
            return nil
        }
        return value
    }
}
