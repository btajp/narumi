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
}
