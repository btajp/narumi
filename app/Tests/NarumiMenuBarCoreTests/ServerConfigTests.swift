import XCTest

@testable import NarumiMenuBarCore

final class ServerConfigTests: XCTestCase {
    let home = URL(fileURLWithPath: "/Users/tester", isDirectory: true)
    let bundleRepo = URL(fileURLWithPath: "/Users/tester/src/narumi", isDirectory: true)
    var bundleURL: URL { bundleRepo.appendingPathComponent("dist/narumi.app") }

    /// A file-existence probe over an explicit set of paths (no file system involved).
    func exists(_ paths: [String]) -> (URL) -> Bool {
        let set = Set(paths)
        return { set.contains($0.standardizedFileURL.path) }
    }

    func markers(in repo: URL) -> [String] {
        ServerConfig.repositoryMarkers.map { repo.appendingPathComponent($0).path }
    }

    func resolve(
        env: [String: String] = [:], stored: String? = nil, bundle: URL? = nil, files: [String] = []
    ) -> ServerConfig {
        ServerConfig.resolve(
            environment: env, storedRepoPath: stored, bundleURL: bundle, homeDirectory: home,
            fileExists: exists(files))
    }

    // MARK: Repository precedence

    func testEnvironmentBeatsDefaultsAndBundle() {
        let config = resolve(
            env: ["NARUMI_REPO": "/env/repo"], stored: "/stored/repo", bundle: bundleURL,
            files: markers(in: bundleRepo))
        XCTAssertEqual(config.repository?.path, "/env/repo")
        XCTAssertEqual(config.repositorySource, .environment)
    }

    func testDefaultsBeatBundle() {
        let config = resolve(stored: "/stored/repo", bundle: bundleURL, files: markers(in: bundleRepo))
        XCTAssertEqual(config.repository?.path, "/stored/repo")
        XCTAssertEqual(config.repositorySource, .userDefaults)
    }

    func testEmptyValuesAreIgnored() {
        let config = resolve(env: ["NARUMI_REPO": ""], stored: "", bundle: bundleURL, files: markers(in: bundleRepo))
        XCTAssertEqual(config.repository, bundleRepo)
        XCTAssertEqual(config.repositorySource, .bundle)
    }

    func testBundleHeuristicRequiresBothMarkers() {
        let onlyRoot = resolve(bundle: bundleURL, files: [bundleRepo.appendingPathComponent("pyproject.toml").path])
        XCTAssertNil(onlyRoot.repository)
        XCTAssertNil(onlyRoot.repositorySource)

        let onlyServer = resolve(bundle: bundleURL, files: [bundleRepo.appendingPathComponent("server/pyproject.toml").path])
        XCTAssertNil(onlyServer.repository)

        let both = resolve(bundle: bundleURL, files: markers(in: bundleRepo))
        XCTAssertEqual(both.repository, bundleRepo)
        XCTAssertEqual(both.repositorySource, .bundle)
    }

    func testBundleOutsideARepositoryIsNotConfigured() {
        let elsewhere = URL(fileURLWithPath: "/Applications/narumi.app")
        let config = resolve(bundle: elsewhere, files: markers(in: bundleRepo))
        XCTAssertNil(config.repository)
        XCTAssertNil(config.repositorySource)
        XCTAssertNil(resolve().repository)
    }

    func testTildeAndRelativeComponentsAreNormalised() {
        let config = resolve(env: ["NARUMI_REPO": "~/src/../src/narumi"])
        XCTAssertEqual(config.repository, URL(fileURLWithPath: NSHomeDirectory() + "/src/narumi", isDirectory: true).standardizedFileURL)
        XCTAssertTrue(config.repository!.hasDirectoryPath)
    }

    func testIsRepository() {
        XCTAssertTrue(ServerConfig.isRepository(bundleRepo, fileExists: exists(markers(in: bundleRepo))))
        XCTAssertFalse(ServerConfig.isRepository(bundleRepo, fileExists: exists([])))
        XCTAssertFalse(
            ServerConfig.isRepository(bundleRepo, fileExists: exists([bundleRepo.appendingPathComponent("pyproject.toml").path])))
    }

    // MARK: Port and URL

    func testDefaultPortAndDerivedURL() {
        let config = resolve()
        XCTAssertEqual(config.port, 8765)
        XCTAssertEqual(config.serverURL.absoluteString, "http://127.0.0.1:8765/mcp")
    }

    func testURLIsDerivedFromPort() {
        let config = resolve(env: ["NARUMI_SERVER_PORT": "9000"])
        XCTAssertEqual(config.port, 9000)
        XCTAssertEqual(config.serverURL.absoluteString, "http://127.0.0.1:9000/mcp")
    }

    func testExplicitURLWinsAndSuppliesThePort() {
        let config = resolve(env: ["NARUMI_SERVER_URL": "http://localhost:9100/mcp"])
        XCTAssertEqual(config.serverURL.absoluteString, "http://localhost:9100/mcp")
        XCTAssertEqual(config.port, 9100)

        let both = resolve(env: ["NARUMI_SERVER_URL": "http://localhost:9100/mcp", "NARUMI_SERVER_PORT": "9200"])
        XCTAssertEqual(both.serverURL.absoluteString, "http://localhost:9100/mcp")
        XCTAssertEqual(both.port, 9200)

        let noPort = resolve(env: ["NARUMI_SERVER_URL": "http://localhost/mcp"])
        XCTAssertEqual(noPort.port, 8765)
    }

    func testInvalidPortAndURLFallBack() {
        XCTAssertEqual(resolve(env: ["NARUMI_SERVER_PORT": "abc"]).port, 8765)
        XCTAssertEqual(resolve(env: ["NARUMI_SERVER_PORT": "70000"]).port, 8765)
        XCTAssertEqual(resolve(env: ["NARUMI_SERVER_PORT": "0"]).port, 8765)
        let garbage = resolve(env: ["NARUMI_SERVER_URL": "not a url"])
        XCTAssertEqual(garbage.serverURL.absoluteString, "http://127.0.0.1:8765/mcp")
    }

    // MARK: Recorder, log, data root

    func testRecorderComesFromTheBundleWhenPresent() {
        let recorder = bundleURL.appendingPathComponent("Contents/MacOS/narumi-recorder").path
        let with = resolve(bundle: bundleURL, files: markers(in: bundleRepo) + [recorder])
        XCTAssertEqual(with.recorder?.path, recorder)
        let without = resolve(bundle: bundleURL, files: markers(in: bundleRepo))
        XCTAssertNil(without.recorder)
        XCTAssertNil(resolve(bundle: nil, files: [recorder]).recorder)
    }

    func testLogFileAndDataRoot() {
        let config = resolve(env: ["NARUMI_HOME": "/Volumes/data/narumi"])
        XCTAssertEqual(config.logFile.path, "/Users/tester/Library/Logs/narumi/server.log")
        XCTAssertEqual(config.dataRoot, "/Volumes/data/narumi")
        XCTAssertNil(resolve(env: ["NARUMI_HOME": ""]).dataRoot)
        XCTAssertNil(resolve().dataRoot)
    }

    // MARK: Runtime mode (repo / bundled)

    var runtimeResources: String { bundleURL.appendingPathComponent("Contents/Resources/runtime").path }

    func testExplicitRepositoryOverridesBundledRuntime() {
        // Only an explicit developer override beats the payload in a release.
        let config = resolve(
            env: ["NARUMI_REPO": "/env/repo"], bundle: bundleURL,
            files: markers(in: bundleRepo) + [runtimeResources])
        XCTAssertEqual(config.runtimeMode, .repo)
        XCTAssertEqual(config.bundledRuntime?.root.path, runtimeResources)

    }

    func testBundledRuntimeBeatsSavedAndInferredRepositories() {
        let dist = resolve(bundle: bundleURL, files: markers(in: bundleRepo) + [runtimeResources])
        XCTAssertEqual(dist.runtimeMode, .bundled)
        XCTAssertEqual(dist.repositorySource, .bundle)

        let installed = URL(fileURLWithPath: "/Applications/narumi.app")
        let saved = resolve(
            stored: "/stale/development/repo", bundle: installed,
            files: [installed.appendingPathComponent("Contents/Resources/runtime").path])
        XCTAssertEqual(saved.runtimeMode, .bundled)
        XCTAssertEqual(saved.repository?.path, "/stale/development/repo", "keep the preference for explicit dev mode")
        XCTAssertTrue(saved.requiresOwnedServer)
    }

    func testLegacyRepoOnlyBuildStillUsesSavedOrInferredRepository() {
        XCTAssertEqual(resolve(stored: "/saved/repo").runtimeMode, .repo)
        XCTAssertEqual(resolve(bundle: bundleURL, files: markers(in: bundleRepo)).runtimeMode, .repo)
    }

    func testBundledModeWhenOnlyRuntimeResourcesExist() {
        let config = resolve(bundle: bundleURL, files: [runtimeResources])
        XCTAssertEqual(config.runtimeMode, .bundled)
        XCTAssertNil(config.repository)
        XCTAssertEqual(config.bundledRuntime?.root.path, runtimeResources)
    }

    func testNeitherMeansNoMode() {
        let config = resolve(bundle: bundleURL, files: [])
        XCTAssertNil(config.runtimeMode)
        XCTAssertNil(config.bundledRuntime)
        XCTAssertNil(resolve().runtimeMode)
    }

    func testRuntimeModeOverride() {
        // Forced bundled beats a resolvable repo.
        let bundled = resolve(
            env: ["NARUMI_RUNTIME_MODE": "bundled", "NARUMI_REPO": "/env/repo"], bundle: bundleURL,
            files: [runtimeResources])
        XCTAssertEqual(bundled.runtimeMode, .bundled)
        XCTAssertEqual(bundled.repository?.path, "/env/repo")

        // Forced repo beats bundled resources — and wins even when no repository resolves
        // (the launcher then reports 未設定 instead of silently switching).
        let repo = resolve(env: ["NARUMI_RUNTIME_MODE": "repo"], bundle: bundleURL, files: [runtimeResources])
        XCTAssertEqual(repo.runtimeMode, .repo)
        XCTAssertNil(repo.repository)

        // Forced bundled without runtime resources: the mode still sticks; the launcher fails
        // visibly because bundledRuntime is nil.
        let missing = resolve(env: ["NARUMI_RUNTIME_MODE": "bundled"], bundle: bundleURL, files: [])
        XCTAssertEqual(missing.runtimeMode, .bundled)
        XCTAssertNil(missing.bundledRuntime)

        // Unknown / empty values fall back to automatic detection.
        XCTAssertEqual(
            resolve(env: ["NARUMI_RUNTIME_MODE": "banana"], bundle: bundleURL, files: [runtimeResources]).runtimeMode,
            .bundled)
        XCTAssertNil(resolve(env: ["NARUMI_RUNTIME_MODE": ""]).runtimeMode)
    }

    func testOnlyExplicitServerURLAllowsBundledServerAdoption() {
        let implicit = resolve(bundle: bundleURL, files: [runtimeResources])
        XCTAssertFalse(implicit.hasExplicitServerURL)
        XCTAssertTrue(implicit.requiresOwnedServer)
        let explicit = resolve(
            env: ["NARUMI_SERVER_URL": "http://localhost:9100/mcp"],
            bundle: bundleURL, files: [runtimeResources])
        XCTAssertTrue(explicit.hasExplicitServerURL)
        XCTAssertFalse(explicit.requiresOwnedServer)
        for invalid in ["", "not a url"] {
            let config = resolve(
                env: ["NARUMI_SERVER_URL": invalid], bundle: bundleURL, files: [runtimeResources])
            XCTAssertFalse(config.hasExplicitServerURL)
            XCTAssertTrue(config.requiresOwnedServer)
        }
        XCTAssertFalse(resolve(env: ["NARUMI_REPO": "/developer/repo"]).requiresOwnedServer)
    }

    func testRuntimePathsFollowTheDataRoot() {
        // Default data root: ~/Library/Application Support/narumi (narumi.config.data_root).
        let config = resolve()
        XCTAssertEqual(
            config.runtimePaths.venv.path,
            "/Users/tester/Library/Application Support/narumi/runtime/venv")
        XCTAssertEqual(
            config.runtimePaths.installedManifest.path,
            "/Users/tester/Library/Application Support/narumi/runtime/installed.json")
        XCTAssertEqual(config.runtimeLogFile.path, "/Users/tester/Library/Logs/narumi/runtime.log")

        // NARUMI_HOME moves the runtime with the data (tilde expanded like the repo path).
        let custom = resolve(env: ["NARUMI_HOME": "/Volumes/データ/narumi home"])
        XCTAssertEqual(custom.runtimePaths.root.path, "/Volumes/データ/narumi home/runtime")
        XCTAssertEqual(custom.runtimePaths.pythonDir.path, "/Volumes/データ/narumi home/runtime/python")
    }
}
