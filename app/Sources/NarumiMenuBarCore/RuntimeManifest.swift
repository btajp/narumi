import Foundation

/// `Contents/Resources/runtime/manifest.json` — what the .app ships for the bundled runtime
/// (uv / Python / wheels / pinned requirements; spec `2026-08-27-narumi-app-distribution-design.md` §1).
///
/// The whole value is compared against `<data root>/runtime/installed.json`: any difference —
/// or a missing / unreadable installed.json — means the venv must be (re)synced. After a
/// successful sync the bundle manifest is copied to installed.json, so "installed == bundled"
/// is exactly "nothing to do".
public struct RuntimeManifest: Codable, Equatable, Sendable {
    public var appVersion: String
    /// Python version for `uv python install` / `uv venv --python` (e.g. "3.13").
    public var python: String
    public var uvVersion: String
    /// Wheel file name (under `runtime/wheels/`) → sha256.
    public var wheels: [String: String]
    public var requirementsSHA256: String

    enum CodingKeys: String, CodingKey {
        case appVersion = "app_version"
        case python
        case uvVersion = "uv_version"
        case wheels
        case requirementsSHA256 = "requirements_sha256"
    }

    public init(
        appVersion: String, python: String, uvVersion: String, wheels: [String: String],
        requirementsSHA256: String
    ) {
        self.appVersion = appVersion
        self.python = python
        self.uvVersion = uvVersion
        self.wheels = wheels
        self.requirementsSHA256 = requirementsSHA256
    }

    /// The bundle side. A bundle without a readable manifest is broken, so this throws.
    public static func load(from url: URL) throws -> RuntimeManifest {
        try JSONDecoder().decode(RuntimeManifest.self, from: Data(contentsOf: url))
    }

    /// The installed side. `nil` (missing, unreadable or undecodable file) simply means
    /// "never synced / unknown state" — the caller re-syncs rather than failing.
    public static func loadInstalled(from url: URL) -> RuntimeManifest? {
        guard let data = try? Data(contentsOf: url) else {
            return nil
        }
        return try? JSONDecoder().decode(RuntimeManifest.self, from: data)
    }

    /// Whether the venv must be (re)built before launching the bundled server.
    public func needsSync(installed: RuntimeManifest?) -> Bool {
        installed != self
    }
}
