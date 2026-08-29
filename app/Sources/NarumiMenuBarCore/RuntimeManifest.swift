import Foundation

public struct CodexRuntimeArtifact: Codable, Equatable, Sendable {
    public var name: String
    public var url: String
    public var sha256: String
    public var size: Int
    public var entry: String

    public init(name: String, url: String, sha256: String, size: Int, entry: String) {
        self.name = name
        self.url = url
        self.sha256 = sha256
        self.size = size
        self.entry = entry
    }
}

public struct CodexRuntimeBinary: Codable, Equatable, Sendable {
    public var path: String
    public var sha256: String
    public var size: Int
    public var architecture: String
    public var versionOutput: String
    public var publisherTeamID: String

    enum CodingKeys: String, CodingKey {
        case path, sha256, size, architecture
        case versionOutput = "version_output"
        case publisherTeamID = "publisher_team_id"
    }

    public init(
        path: String, sha256: String, size: Int, architecture: String,
        versionOutput: String, publisherTeamID: String
    ) {
        self.path = path
        self.sha256 = sha256
        self.size = size
        self.architecture = architecture
        self.versionOutput = versionOutput
        self.publisherTeamID = publisherTeamID
    }
}

public struct CodexRuntimeLicense: Codable, Equatable, Sendable {
    public var spdx: String
    public var path: String
    public var source: String
    public var sourceTag: String
    public var sha256: String
    public var size: Int
    public var noticePath: String
    public var noticeSource: String
    public var noticeSHA256: String
    public var noticeSize: Int

    enum CodingKeys: String, CodingKey {
        case spdx, path, source, sha256, size
        case sourceTag = "source_tag"
        case noticePath = "notice_path"
        case noticeSource = "notice_source"
        case noticeSHA256 = "notice_sha256"
        case noticeSize = "notice_size"
    }

    public init(
        spdx: String, path: String, source: String, sourceTag: String,
        sha256: String, size: Int, noticePath: String, noticeSource: String,
        noticeSHA256: String, noticeSize: Int
    ) {
        self.spdx = spdx
        self.path = path
        self.source = source
        self.sourceTag = sourceTag
        self.sha256 = sha256
        self.size = size
        self.noticePath = noticePath
        self.noticeSource = noticeSource
        self.noticeSHA256 = noticeSHA256
        self.noticeSize = noticeSize
    }
}

public struct CodexRuntimePayload: Codable, Equatable, Sendable {
    public var version: String
    public var source: String
    public var sourceTag: String
    public var sourceCommit: String
    public var artifact: CodexRuntimeArtifact
    public var binary: CodexRuntimeBinary
    public var license: CodexRuntimeLicense

    enum CodingKeys: String, CodingKey {
        case version, source, artifact, binary, license
        case sourceTag = "source_tag"
        case sourceCommit = "source_commit"
    }

    public init(
        version: String, source: String, sourceTag: String, sourceCommit: String,
        artifact: CodexRuntimeArtifact, binary: CodexRuntimeBinary,
        license: CodexRuntimeLicense
    ) {
        self.version = version
        self.source = source
        self.sourceTag = sourceTag
        self.sourceCommit = sourceCommit
        self.artifact = artifact
        self.binary = binary
        self.license = license
    }
}

/// `Contents/Resources/runtime/manifest.json` — what the .app ships for the bundled runtime
/// (uv / Python / wheels / pinned requirements; spec `2026-08-27-narumi-app-distribution-design.md` §1).
///
/// The whole value is compared against `<data root>/runtime/installed.json`: any difference —
/// or a missing / unreadable installed.json — means the venv must be (re)synced. After a
/// successful sync AND server identity check the bundle manifest is copied to installed.json.
/// The previous marker remains recoverable until the installation transaction commits.
public struct RuntimeManifest: Codable, Equatable, Sendable {
    public var appVersion: String
    /// Python version for `uv python install` / `uv venv --python` (e.g. "3.13").
    public var python: String
    public var uvVersion: String
    /// Wheel file name (under `runtime/wheels/`) → sha256.
    public var wheels: [String: String]
    public var requirementsSHA256: String
    /// Fixed OpenAI Codex payload shipped in distribution builds. Older development
    /// manifests may omit it; release inventory requires it for `--runtime` artifacts.
    public var codex: CodexRuntimePayload?

    enum CodingKeys: String, CodingKey {
        case appVersion = "app_version"
        case python
        case uvVersion = "uv_version"
        case wheels
        case requirementsSHA256 = "requirements_sha256"
        case codex
    }

    public init(
        appVersion: String, python: String, uvVersion: String, wheels: [String: String],
        requirementsSHA256: String, codex: CodexRuntimePayload? = nil
    ) {
        self.appVersion = appVersion
        self.python = python
        self.uvVersion = uvVersion
        self.wheels = wheels
        self.requirementsSHA256 = requirementsSHA256
        self.codex = codex
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
