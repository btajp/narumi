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
        env: [String: String] = [:], stored: String? = nil, bundle: URL? = nil, files: [String] = [],
        canonicalize: (URL) -> URL = { $0.standardizedFileURL }
    ) -> ServerConfig {
        ServerConfig.resolve(
            environment: env, storedRepoPath: stored, bundleURL: bundle, homeDirectory: home,
            fileExists: exists(files), canonicalize: canonicalize)
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
        XCTAssertEqual(config.serverURL.absoluteString, "https://127.0.0.1:8765/mcp")
        XCTAssertNoThrow(try config.validateSecureEndpoint())
    }

    func testURLIsDerivedFromPort() {
        let config = resolve(env: ["NARUMI_SERVER_PORT": "9000"])
        XCTAssertEqual(config.port, 9000)
        XCTAssertEqual(config.serverURL.absoluteString, "https://127.0.0.1:9000/mcp")
        XCTAssertNoThrow(try config.validateSecureEndpoint())
    }

    func testExplicitURLWinsAndSuppliesThePort() {
        let config = resolve(env: ["NARUMI_SERVER_URL": "https://127.0.0.1:9100/mcp"])
        XCTAssertEqual(config.serverURL.absoluteString, "https://127.0.0.1:9100/mcp")
        XCTAssertEqual(config.port, 9100)
        XCTAssertNoThrow(try config.validateSecureEndpoint())

        let both = resolve(env: ["NARUMI_SERVER_URL": "https://127.0.0.1:9100/mcp", "NARUMI_SERVER_PORT": "9200"])
        XCTAssertEqual(both.serverURL.absoluteString, "https://127.0.0.1:9100/mcp")
        XCTAssertEqual(both.port, 9200)
        XCTAssertThrowsError(try both.validateSecureEndpoint()) { error in
            XCTAssertEqual(error as? MCPConnectionError, .endpointMismatch)
        }

        let noPort = resolve(env: ["NARUMI_SERVER_URL": "https://127.0.0.1/mcp"])
        XCTAssertEqual(noPort.port, 8765)
        XCTAssertThrowsError(try noPort.validateSecureEndpoint())
    }

    func testInvalidPortFallsBackToDefault() {
        XCTAssertEqual(resolve(env: ["NARUMI_SERVER_PORT": "abc"]).port, 8765)
        XCTAssertEqual(resolve(env: ["NARUMI_SERVER_PORT": "70000"]).port, 8765)
        XCTAssertEqual(resolve(env: ["NARUMI_SERVER_PORT": "0"]).port, 8765)
    }

    func testInvalidExplicitURLFailsInsteadOfSilentlyUsingDefaultEndpoint() {
        for value in [
            "not a url", "https://[", "http://127.0.0.1:8765/mcp",
            "https://localhost:8765/mcp", "https://example.com:8765/mcp",
            "https://127.0.0.1:8765/mcp?token=fixture", "https://127.0.0.1:8765/other",
        ] {
            let config = resolve(env: ["NARUMI_SERVER_URL": value])
            XCTAssertTrue(config.hasExplicitServerURL, value)
            XCTAssertNotEqual(config.serverURL, resolve().serverURL, value)
            XCTAssertThrowsError(try config.validateSecureEndpoint(), value) { error in
                XCTAssertEqual(error as? MCPConnectionError, .invalidEndpoint, value)
            }
        }
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
        XCTAssertEqual(config.bootstrapDataRoot.path, "/Volumes/data/narumi")
        XCTAssertNil(resolve(env: ["NARUMI_HOME": ""]).dataRoot)
        XCTAssertNil(resolve().dataRoot)
        XCTAssertEqual(resolve().bootstrapDataRoot.path, "/Users/tester/Library/Application Support/narumi")
    }

    func testRepositoryKeychainHelperPrefersItsAppBeforeRepositoryBuild() {
        let helper = bundleURL.appendingPathComponent(ServerConfig.keychainHelperPathInBundle).path
        let release = bundleRepo.appendingPathComponent("app/.build/release/narumi-keychain").path
        let config = resolve(stored: bundleRepo.path, files: [helper, release])
        XCTAssertEqual(config.runtimeMode, .repo)
        XCTAssertEqual(config.keychainHelper?.path, helper)
        XCTAssertFalse(config.rejectedKeychainHelperOverride)
        XCTAssertEqual(config.keychainHelperLocations.map(\.candidate.path), [
            helper, release, bundleRepo.appendingPathComponent("app/.build/debug/narumi-keychain").path,
        ])
        XCTAssertTrue(config.keychainHelperLocations.allSatisfy { $0.trustedRoot == bundleRepo })
    }

    func testRepositoryKeychainHelperPrefersReleaseAndUsesDebugIfNeeded() {
        let release = bundleRepo.appendingPathComponent("app/.build/release/narumi-keychain").path
        let debug = bundleRepo.appendingPathComponent("app/.build/debug/narumi-keychain").path
        XCTAssertEqual(resolve(stored: bundleRepo.path, files: [release, debug]).keychainHelper?.path, release)
        XCTAssertEqual(resolve(stored: bundleRepo.path, files: [debug]).keychainHelper?.path, debug)
        XCTAssertNil(resolve(stored: bundleRepo.path).keychainHelper)
    }

    func testBundledKeychainHelperNeverFallsBackToSavedRepositoryOrAnotherBundle() {
        let installed = URL(fileURLWithPath: "/Applications/narumi.app")
        let helper = installed.appendingPathComponent(ServerConfig.keychainHelperPathInBundle)
        let runtime = installed.appendingPathComponent(BundledRuntime.bundleSubpath).path
        let repositoryHelper = bundleURL.appendingPathComponent(ServerConfig.keychainHelperPathInBundle).path
        let repositoryBuild = bundleRepo.appendingPathComponent("app/.build/release/narumi-keychain").path
        let alternatives = [runtime, repositoryHelper, repositoryBuild]
        let config = resolve(stored: bundleRepo.path, bundle: installed, files: alternatives + [helper.path])
        XCTAssertEqual(config.runtimeMode, .bundled)
        XCTAssertEqual(config.keychainHelper, helper)
        XCTAssertEqual(config.keychainHelperLocations, [.init(trustedRoot: installed, candidate: helper)])

        let missing = resolve(stored: bundleRepo.path, bundle: installed, files: alternatives)
        XCTAssertNil(missing.keychainHelper)
        XCTAssertFalse(missing.rejectedKeychainHelperOverride)
        for disallowed in [repositoryHelper, repositoryBuild] {
            let explicit = resolve(
                env: ["NARUMI_KEYCHAIN_HELPER": disallowed], stored: bundleRepo.path,
                bundle: installed, files: alternatives + [helper.path])
            assertRejectedHelper(explicit)
        }
    }

    func testExplicitKeychainHelperAcceptsOnlyCurrentModeKnownCandidates() {
        let repoCandidates = [
            bundleURL.appendingPathComponent(ServerConfig.keychainHelperPathInBundle),
            bundleRepo.appendingPathComponent("app/.build/release/narumi-keychain"),
            bundleRepo.appendingPathComponent("app/.build/debug/narumi-keychain"),
        ]
        for candidate in repoCandidates {
            let config = resolve(
                env: ["NARUMI_KEYCHAIN_HELPER": candidate.path], stored: bundleRepo.path,
                files: repoCandidates.map(\.path))
            XCTAssertEqual(config.keychainHelper, candidate)
            XCTAssertFalse(config.rejectedKeychainHelperOverride)
            XCTAssertNoThrow(try config.validateSecureEndpoint())
        }
        let installed = URL(fileURLWithPath: "/Applications/narumi.app")
        let helper = installed.appendingPathComponent(ServerConfig.keychainHelperPathInBundle)
        let config = resolve(
            env: ["NARUMI_KEYCHAIN_HELPER": helper.path], bundle: installed,
            files: [installed.appendingPathComponent(BundledRuntime.bundleSubpath).path, helper.path])
        XCTAssertEqual(config.keychainHelper, helper)
        XCTAssertFalse(config.rejectedKeychainHelperOverride)
        XCTAssertNoThrow(try config.validateSecureEndpoint())
    }

    func testExplicitRepoModeDoesNotSelectTheRunningApplicationHelper() {
        let installed = URL(fileURLWithPath: "/Applications/narumi.app")
        let helper = installed.appendingPathComponent(ServerConfig.keychainHelperPathInBundle).path
        let config = resolve(
            env: ["NARUMI_REPO": bundleRepo.path, "NARUMI_KEYCHAIN_HELPER": helper], bundle: installed,
            files: [installed.appendingPathComponent(BundledRuntime.bundleSubpath).path, helper])
        XCTAssertEqual(config.runtimeMode, .repo)
        assertRejectedHelper(config)
    }

    func testArbitraryAndRelativeHelperOverridesFailWithoutFallingBack() {
        let known = bundleRepo.appendingPathComponent("app/.build/release/narumi-keychain").path
        for requested in [
            "/custom/helper", "/usr/bin/security", "/other/narumi.app/Contents/MacOS/narumi-keychain",
            "app/.build/release/narumi-keychain", "./narumi-keychain", "../narumi-keychain",
            "~/tools/narumi-keychain",
        ] {
            let config = resolve(
                env: ["NARUMI_KEYCHAIN_HELPER": requested], stored: bundleRepo.path, files: [known, requested])
            assertRejectedHelper(config)
        }
        let unconfigured = resolve(env: ["NARUMI_KEYCHAIN_HELPER": known], files: [known])
        XCTAssertTrue(unconfigured.keychainHelperLocations.isEmpty)
        assertRejectedHelper(unconfigured)
        let empty = resolve(env: ["NARUMI_KEYCHAIN_HELPER": ""], stored: bundleRepo.path, files: [known])
        XCTAssertEqual(empty.keychainHelper?.path, known)
        XCTAssertFalse(empty.rejectedKeychainHelperOverride)
    }

    func testCanonicalHelperAliasReturnsTheOriginalKnownCandidateSpelling() {
        let known = bundleRepo.appendingPathComponent("app/.build/release/narumi-keychain")
        let resolved = bundleRepo.appendingPathComponent("app/.build/arm64-apple-macosx/release/narumi-keychain")
        let alias = URL(fileURLWithPath: "/fixture/alias/narumi-keychain")
        let canonicalize: (URL) -> URL = { url in
            [known, alias].contains(url) ? resolved : url.standardizedFileURL
        }
        for requested in [resolved, alias] {
            let config = resolve(
                env: ["NARUMI_KEYCHAIN_HELPER": requested.path], stored: bundleRepo.path,
                files: [known.path], canonicalize: canonicalize)
            XCTAssertEqual(config.keychainHelper, known)
            XCTAssertNotEqual(config.keychainHelper, requested)
            XCTAssertFalse(config.rejectedKeychainHelperOverride)
            XCTAssertNoThrow(try config.validateSecureEndpoint())
        }
        let relative = resolve(
            env: ["NARUMI_KEYCHAIN_HELPER": "app/.build/release/narumi-keychain"], stored: bundleRepo.path,
            files: [known.path], canonicalize: { _ in resolved })
        assertRejectedHelper(relative)
    }

    func testBundledCanonicalAliasCannotReplaceTheTrustedCandidateSpelling() {
        let installed = URL(fileURLWithPath: "/Applications/narumi.app")
        let helper = installed.appendingPathComponent(ServerConfig.keychainHelperPathInBundle)
        let alias = URL(fileURLWithPath: "/fixture/alias/narumi-keychain")
        let config = resolve(
            env: ["NARUMI_KEYCHAIN_HELPER": alias.path], bundle: installed,
            files: [installed.appendingPathComponent(BundledRuntime.bundleSubpath).path, helper.path],
            canonicalize: { $0 == alias ? helper : $0.standardizedFileURL })
        XCTAssertEqual(config.keychainHelper, helper)
        XCTAssertFalse(config.rejectedKeychainHelperOverride)
        XCTAssertEqual(config.keychainHelperLocations, [.init(trustedRoot: installed, candidate: helper)])
        XCTAssertNoThrow(try config.validateSecureEndpoint())
    }

    private func assertRejectedHelper(
        _ config: ServerConfig, file: StaticString = #filePath, line: UInt = #line
    ) {
        XCTAssertTrue(config.rejectedKeychainHelperOverride, file: file, line: line)
        XCTAssertNil(config.keychainHelper, file: file, line: line)
        XCTAssertThrowsError(try config.validateSecureEndpoint(), file: file, line: line) { error in
            XCTAssertEqual(error as? MCPConnectionError, .credentialUnavailable, file: file, line: line)
        }
        XCTAssertThrowsError(try config.validatedKeychainHelper(), file: file, line: line) { error in
            XCTAssertEqual(error as? MCPConnectionError, .credentialUnavailable, file: file, line: line)
        }
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
            env: ["NARUMI_SERVER_URL": "https://127.0.0.1:9100/mcp"],
            bundle: bundleURL, files: [runtimeResources])
        XCTAssertTrue(explicit.hasExplicitServerURL)
        XCTAssertFalse(explicit.requiresOwnedServer)
        XCTAssertNoThrow(try explicit.validateSecureEndpoint())
        let empty = resolve(env: ["NARUMI_SERVER_URL": ""], bundle: bundleURL, files: [runtimeResources])
        XCTAssertFalse(empty.hasExplicitServerURL)
        XCTAssertTrue(empty.requiresOwnedServer)
        // An invalid explicit choice stays visible, but cannot authorize any connection.
        let invalid = resolve(env: ["NARUMI_SERVER_URL": "not a url"], bundle: bundleURL, files: [runtimeResources])
        XCTAssertTrue(invalid.hasExplicitServerURL)
        XCTAssertThrowsError(try invalid.validateSecureEndpoint())
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
        XCTAssertEqual(custom.bootstrapDataRoot.path, "/Volumes/データ/narumi home")
    }
}
