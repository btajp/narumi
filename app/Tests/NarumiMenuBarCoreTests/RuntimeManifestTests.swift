import XCTest

@testable import NarumiMenuBarCore

final class RuntimeManifestTests: XCTestCase {
    /// The shape `scripts/build-app.sh --runtime` writes.
    static let sampleJSON = """
        {
          "app_version": "0.1.0",
          "python": "3.13",
          "uv_version": "0.11.25",
          "wheels": {
            "narumi-0.1.0-py3-none-any.whl": "aaaa",
            "narumi_server-0.1.0-py3-none-any.whl": "bbbb"
          },
          "requirements_sha256": "cccc",
          "codex": {
            "version": "0.150.1",
            "source": "https://github.com/openai/codex/releases/tag/rust-v0.150.1",
            "source_tag": "rust-v0.150.1",
            "source_commit": "90854393966b21e9ebfd21b122334eb09a20c93d",
            "artifact": {
              "name": "codex-aarch64-apple-darwin.tar.gz",
              "url": "https://github.com/openai/codex/releases/download/rust-v0.150.1/codex-aarch64-apple-darwin.tar.gz",
              "sha256": "artifact-hash",
              "size": 91484322,
              "entry": "codex-aarch64-apple-darwin"
            },
            "binary": {
              "path": "codex/0.150.1/codex",
              "sha256": "binary-hash",
              "size": 228986048,
              "architecture": "arm64",
              "version_output": "codex-cli 0.150.1",
              "publisher_team_id": "2DC432GLL2"
            },
            "license": {
              "spdx": "Apache-2.0",
              "path": "licenses/openai-codex-Apache-2.0.txt",
              "source": "https://github.com/openai/codex/blob/rust-v0.150.1/LICENSE",
              "source_tag": "rust-v0.150.1",
              "sha256": "license-hash",
              "size": 10926,
              "notice_path": "licenses/openai-codex-NOTICE.txt",
              "notice_source": "https://github.com/openai/codex/blob/rust-v0.150.1/NOTICE",
              "notice_sha256": "notice-hash",
              "notice_size": 242
            }
          }
        }
        """

    static let codex = CodexRuntimePayload(
        version: "0.150.1",
        source: "https://github.com/openai/codex/releases/tag/rust-v0.150.1",
        sourceTag: "rust-v0.150.1", sourceCommit: "90854393966b21e9ebfd21b122334eb09a20c93d",
        artifact: CodexRuntimeArtifact(
            name: "codex-aarch64-apple-darwin.tar.gz",
            url: "https://github.com/openai/codex/releases/download/rust-v0.150.1/codex-aarch64-apple-darwin.tar.gz",
            sha256: "artifact-hash", size: 91_484_322, entry: "codex-aarch64-apple-darwin"),
        binary: CodexRuntimeBinary(
            path: "codex/0.150.1/codex", sha256: "binary-hash", size: 228_986_048,
            architecture: "arm64", versionOutput: "codex-cli 0.150.1",
            publisherTeamID: "2DC432GLL2"),
        license: CodexRuntimeLicense(
            spdx: "Apache-2.0", path: "licenses/openai-codex-Apache-2.0.txt",
            source: "https://github.com/openai/codex/blob/rust-v0.150.1/LICENSE",
            sourceTag: "rust-v0.150.1", sha256: "license-hash", size: 10_926,
            noticePath: "licenses/openai-codex-NOTICE.txt",
            noticeSource: "https://github.com/openai/codex/blob/rust-v0.150.1/NOTICE",
            noticeSHA256: "notice-hash", noticeSize: 242))

    var sample: RuntimeManifest {
        RuntimeManifest(
            appVersion: "0.1.0", python: "3.13", uvVersion: "0.11.25",
            wheels: [
                "narumi-0.1.0-py3-none-any.whl": "aaaa",
                "narumi_server-0.1.0-py3-none-any.whl": "bbbb",
            ],
            requirementsSHA256: "cccc", codex: Self.codex)
    }

    func write(_ contents: String, name: String = "manifest.json") throws -> URL {
        let dir = FileManager.default.temporaryDirectory
            .appendingPathComponent("narumi-manifest-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        addTeardownBlock {
            try? FileManager.default.removeItem(at: dir)
        }
        let url = dir.appendingPathComponent(name)
        try Data(contents.utf8).write(to: url)
        return url
    }

    func testDecodesSnakeCaseKeys() throws {
        let manifest = try RuntimeManifest.load(from: write(Self.sampleJSON))
        XCTAssertEqual(manifest, sample)
    }

    func testLoadThrowsOnMissingOrBrokenBundleManifest() throws {
        XCTAssertThrowsError(
            try RuntimeManifest.load(from: URL(fileURLWithPath: "/nonexistent/manifest.json")))
        XCTAssertThrowsError(try RuntimeManifest.load(from: write("{not json")))
        // Missing keys are an error too (a half-written bundle must not look synced).
        XCTAssertThrowsError(try RuntimeManifest.load(from: write("{\"app_version\": \"0.1.0\"}")))
    }

    func testLoadInstalledReturnsNilInsteadOfThrowing() throws {
        XCTAssertNil(RuntimeManifest.loadInstalled(from: URL(fileURLWithPath: "/nonexistent/installed.json")))
        XCTAssertNil(RuntimeManifest.loadInstalled(from: try write("{broken", name: "installed.json")))
        XCTAssertEqual(
            RuntimeManifest.loadInstalled(from: try write(Self.sampleJSON, name: "installed.json")),
            sample)
    }

    func testEqualityDecidesSync() {
        XCTAssertTrue(sample.needsSync(installed: nil), "never synced → sync")
        XCTAssertFalse(sample.needsSync(installed: sample), "identical → skip")

        var newWheel = sample
        newWheel.wheels["narumi-0.1.0-py3-none-any.whl"] = "dddd"
        XCTAssertTrue(sample.needsSync(installed: newWheel))

        var newRequirements = sample
        newRequirements.requirementsSHA256 = "eeee"
        XCTAssertTrue(sample.needsSync(installed: newRequirements))

        var newPython = sample
        newPython.python = "3.14"
        XCTAssertTrue(sample.needsSync(installed: newPython))

        var newUV = sample
        newUV.uvVersion = "0.12.0"
        XCTAssertTrue(sample.needsSync(installed: newUV))

        var newApp = sample
        newApp.appVersion = "0.2.0"
        XCTAssertTrue(sample.needsSync(installed: newApp))

        var newCodex = sample
        newCodex.codex?.binary.sha256 = "changed-binary"
        XCTAssertTrue(sample.needsSync(installed: newCodex))
    }

    /// A post-readiness commit copies the bundle manifest byte-for-byte, so the decoded
    /// values must round-trip to equality regardless of key order / formatting.
    func testInstalledCopyOfBundleManifestComparesEqual() throws {
        let codexJSON = try XCTUnwrap(
            String(data: JSONEncoder().encode(Self.codex), encoding: .utf8))
        let reordered = """
            {"requirements_sha256":"cccc","python":"3.13","uv_version":"0.11.25",
             "wheels":{"narumi_server-0.1.0-py3-none-any.whl":"bbbb",
                       "narumi-0.1.0-py3-none-any.whl":"aaaa"},
             "codex":\(codexJSON),
             "app_version":"0.1.0"}
            """
        let bundled = try RuntimeManifest.load(from: write(Self.sampleJSON))
        let installed = RuntimeManifest.loadInstalled(from: try write(reordered, name: "installed.json"))
        XCTAssertEqual(installed, bundled)
        XCTAssertFalse(bundled.needsSync(installed: installed))
    }
}
