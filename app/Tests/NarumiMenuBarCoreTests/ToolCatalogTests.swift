import XCTest

@testable import NarumiMenuBarCore

/// Surface parity: every tool name the app calls must exist in `contracts/manifest.json`
/// (app ⊆ contract; `docs/superpowers/specs/2026-08-27-narumi-surface-parity-design.md`).
/// Reads the manifest from the repository checkout — no network, no server.
final class ToolCatalogTests: XCTestCase {
    private struct Manifest: Decodable {
        let name: String
        let contractVersion: String
        let tools: [String]

        enum CodingKeys: String, CodingKey {
            case name
            case contractVersion = "contract_version"
            case tools
        }
    }

    private struct ManifestNotFound: Error {}

    /// Walk up from this source file to the directory containing `contracts/manifest.json`.
    private func manifestURL() throws -> URL {
        var directory = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
        while true {
            let candidate = directory.appendingPathComponent("contracts/manifest.json")
            if FileManager.default.fileExists(atPath: candidate.path) {
                return candidate
            }
            let parent = directory.deletingLastPathComponent()
            guard parent.path != directory.path else {
                XCTFail("contracts/manifest.json not found above \(#filePath)")
                throw ManifestNotFound()
            }
            directory = parent
        }
    }

    private func loadManifest() throws -> Manifest {
        let url = try manifestURL()
        let data = try Data(contentsOf: url)
        do {
            return try JSONDecoder().decode(Manifest.self, from: data)
        } catch {
            XCTFail("could not decode \(url.path): \(error)")
            throw error
        }
    }

    func testAllUsedIsNonEmpty() {
        XCTAssertFalse(ToolCatalog.allUsed.isEmpty, "the app calls at least one tool")
    }

    func testAllUsedHasNoDuplicates() {
        let duplicates = Dictionary(grouping: ToolCatalog.allUsed, by: { $0 })
            .filter { $0.value.count > 1 }.keys.sorted()
        XCTAssertTrue(duplicates.isEmpty, "duplicate names in ToolCatalog.allUsed: \(duplicates)")
    }

    func testEveryUsedToolExistsInContractManifest() throws {
        let manifest = try loadManifest()
        XCTAssertEqual(manifest.name, "narumi")
        let contractTools = Set(manifest.tools)
        for name in ToolCatalog.allUsed {
            XCTAssertTrue(
                contractTools.contains(name),
                "\(name) is called by the app but missing from contracts/manifest.json "
                    + "(contract_version \(manifest.contractVersion))")
        }
    }

    func testEveryContractToolHasAnAppSurface() throws {
        let manifest = try loadManifest()
        XCTAssertEqual(ToolCatalog.allUsed.count, 27)
        XCTAssertEqual(Set(ToolCatalog.allUsed), Set(manifest.tools))
    }

    func testNamedConstantsAreListedInAllUsed() {
        // Constants exist so call sites never use bare literals; each one must also be
        // registered in `allUsed`, or the parity check silently skips it.
        for name in [
            ToolCatalog.startRecording, ToolCatalog.stopRecording, ToolCatalog.getServerInfo,
            ToolCatalog.getGaiaConnection, ToolCatalog.setGaiaConnection, ToolCatalog.testGaiaConnection,
        ] {
            XCTAssertTrue(ToolCatalog.allUsed.contains(name), "\(name) missing from ToolCatalog.allUsed")
        }
    }
}
