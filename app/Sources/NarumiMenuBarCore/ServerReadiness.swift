import Foundation

/// Readiness of an owned bundled process is more than an HTTP response: a stale checkout
/// can answer on the same port. Compare the existing diagnostic contract with our bundle.
public struct BundledServerIdentity: Equatable, Sendable {
    public var serverVersion: String
    public var contractVersion: String
    public var recorder: URL
    public var contractsDirectory: URL
    public var dataRoot: URL

    public struct Mismatch: LocalizedError, Equatable {
        public let field: String
        public var errorDescription: String? {
            "起動したサーバーの \(field) が同梱アプリと一致しません。接続を中止しました"
        }
    }

    public init(
        serverVersion: String, contractVersion: String, recorder: URL,
        contractsDirectory: URL, dataRoot: URL
    ) {
        self.serverVersion = serverVersion
        self.contractVersion = contractVersion
        self.recorder = recorder
        self.contractsDirectory = contractsDirectory
        self.dataRoot = dataRoot
    }

    public static func load(config: ServerConfig, manifest: RuntimeManifest) throws -> BundledServerIdentity {
        struct ContractManifest: Decodable {
            var contract_version: String
        }
        guard let runtime = config.bundledRuntime, let recorder = config.recorder,
            FileManager.default.isExecutableFile(atPath: recorder.path)
        else {
            throw Mismatch(field: "recorder")
        }
        let contracts = try JSONDecoder().decode(
            ContractManifest.self,
            from: Data(contentsOf: runtime.contractsDir.appendingPathComponent("manifest.json")))
        return BundledServerIdentity(
            serverVersion: manifest.appVersion, contractVersion: contracts.contract_version,
            recorder: recorder, contractsDirectory: runtime.contractsDir,
            dataRoot: config.runtimePaths.root.deletingLastPathComponent())
    }

    public func validate(_ info: ServerInfo) throws {
        guard RecordingPermissionContract.supportsSetup(info.contractVersion),
            info.secureTransport?.mode == "pinned_tls",
            info.secureTransport?.tlsRequired == true,
            info.secureTransport?.clientAuthRequired == true
        else {
            throw MCPConnectionError.incompatibleContract
        }
        guard info.name == "narumi" else { throw Mismatch(field: "name") }
        guard info.serverVersion == serverVersion else { throw Mismatch(field: "server_version") }
        guard info.contractVersion == contractVersion else { throw Mismatch(field: "contract_version") }
        guard sameFile(info.diagnostics.recorderPath, recorder) else { throw Mismatch(field: "recorder_path") }
        guard sameFile(info.diagnostics.contractsDir, contractsDirectory) else { throw Mismatch(field: "contracts_dir") }
        guard sameFile(info.diagnostics.dataRoot, dataRoot) else { throw Mismatch(field: "data_root") }
    }

    private func sameFile(_ reportedPath: String?, _ expected: URL) -> Bool {
        guard let reportedPath, reportedPath.hasPrefix("/") else { return false }
        return URL(fileURLWithPath: reportedPath).standardizedFileURL.resolvingSymlinksInPath().path
            == expected.standardizedFileURL.resolvingSymlinksInPath().path
    }
}
