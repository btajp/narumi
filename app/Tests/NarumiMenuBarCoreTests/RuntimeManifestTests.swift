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
          "requirements_sha256": "cccc"
        }
        """

    var sample: RuntimeManifest {
        RuntimeManifest(
            appVersion: "0.1.0", python: "3.13", uvVersion: "0.11.25",
            wheels: [
                "narumi-0.1.0-py3-none-any.whl": "aaaa",
                "narumi_server-0.1.0-py3-none-any.whl": "bbbb",
            ],
            requirementsSHA256: "cccc")
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
    }

    /// A post-readiness commit copies the bundle manifest byte-for-byte, so the decoded
    /// values must round-trip to equality regardless of key order / formatting.
    func testInstalledCopyOfBundleManifestComparesEqual() throws {
        let reordered = """
            {"requirements_sha256":"cccc","python":"3.13","uv_version":"0.11.25",
             "wheels":{"narumi_server-0.1.0-py3-none-any.whl":"bbbb",
                       "narumi-0.1.0-py3-none-any.whl":"aaaa"},
             "app_version":"0.1.0"}
            """
        let bundled = try RuntimeManifest.load(from: write(Self.sampleJSON))
        let installed = RuntimeManifest.loadInstalled(from: try write(reordered, name: "installed.json"))
        XCTAssertEqual(installed, bundled)
        XCTAssertFalse(bundled.needsSync(installed: installed))
    }
}
