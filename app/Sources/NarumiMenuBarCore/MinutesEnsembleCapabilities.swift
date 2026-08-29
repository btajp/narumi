import Foundation

public struct MinutesEnsembleLimits: Codable, Equatable, Sendable {
    public let maxGenerators: Int
    public let maxConcurrency: Int
    public let maxGenerationAttemptsPerRun: Int
    public let inputModalities: [String]
    public let maxReductionDepth: Int

    enum CodingKeys: String, CodingKey, CaseIterable {
        case maxGenerators = "max_generators"
        case maxConcurrency = "max_concurrency"
        case maxGenerationAttemptsPerRun = "max_generation_attempts_per_run"
        case inputModalities = "input_modalities"
        case maxReductionDepth = "max_reduction_depth"
    }

    public init(
        maxGenerators: Int = 4, maxConcurrency: Int = 1,
        maxGenerationAttemptsPerRun: Int = 64, inputModalities: [String] = ["text"],
        maxReductionDepth: Int = 6
    ) {
        self.maxGenerators = maxGenerators
        self.maxConcurrency = maxConcurrency
        self.maxGenerationAttemptsPerRun = maxGenerationAttemptsPerRun
        self.inputModalities = inputModalities
        self.maxReductionDepth = maxReductionDepth
    }

    public init(from decoder: Decoder) throws {
        try requireContractKeys(
            decoder, allowed: Set(CodingKeys.allCases.map(\.rawValue)),
            required: Set(CodingKeys.allCases.map(\.rawValue)))
        let container = try decoder.container(keyedBy: CodingKeys.self)
        maxGenerators = try container.decode(Int.self, forKey: .maxGenerators)
        maxConcurrency = try container.decode(Int.self, forKey: .maxConcurrency)
        maxGenerationAttemptsPerRun = try container.decode(Int.self, forKey: .maxGenerationAttemptsPerRun)
        inputModalities = try container.decode([String].self, forKey: .inputModalities)
        maxReductionDepth = try container.decode(Int.self, forKey: .maxReductionDepth)
        guard isSupportedBaseline else {
            throw DecodingError.dataCorruptedError(
                forKey: .maxGenerators, in: container, debugDescription: "Unsupported ensemble limits")
        }
    }

    public var isSupportedBaseline: Bool {
        maxGenerators == 4 && maxConcurrency == 1 && maxGenerationAttemptsPerRun == 64
            && inputModalities == ["text"] && maxReductionDepth == 6
    }
}

extension ServerCapabilities {
    /// Whether the negotiated wire schema accepts `minutes_ensemble`.
    /// This deliberately ignores runtime readiness so a v6 client can still
    /// clear a saved ensemble while execution is temporarily unavailable.
    public func supportsMinutesEnsembleWire(contractVersion: String?) -> Bool {
        guard let version = contractVersion,
            RecordingPermissionContract.supportsSetup(version),
            version.split(separator: ".").first == "6" else { return false }
        return true
    }

    public func canExecuteMinutesEnsemble(contractVersion: String?) -> Bool {
        guard supportsMinutesEnsembleWire(contractVersion: contractVersion),
            transports.contains("streamable-http"),
            workflow?.ensembleGeneration == true,
            minutesEnsembleLimits?.isSupportedBaseline == true else { return false }
        return !supportedMinutesModelProviders(contractVersion: contractVersion).isEmpty
    }

    public func supportsMinutesEnsemble(contractVersion: String?) -> Bool {
        canExecuteMinutesEnsemble(contractVersion: contractVersion)
    }
}

public enum MinutesEnsembleExecutionAvailability {
    public static func unavailableReason(
        capabilities: ServerCapabilities?, contractVersion: String?, supportedProviders: [String]
    ) -> String? {
        guard let capabilities else { return "サーバーの複数案生成への対応をまだ確認できません。" }
        guard capabilities.supportsMinutesEnsembleWire(contractVersion: contractVersion) else {
            return "この契約では複数案生成の設定に対応していません。"
        }
        guard capabilities.minutesEnsembleLimits?.isSupportedBaseline == true else {
            return "サーバーが対応する複数案生成の上限を公開していません。保存済み設定は確認できますが、新しい実行には使えません。"
        }
        guard capabilities.transports.contains("streamable-http") else {
            return "複数案生成は認証済みの常駐サーバー接続でのみ実行できます。"
        }
        guard capabilities.workflow?.ensembleGeneration == true else {
            return "サーバーが複数案生成の実行能力を公開していません。保存済み設定は確認できますが、新しい実行には使えません。"
        }
        guard !supportedProviders.isEmpty else {
            return "複数案生成に利用できる議事録プロバイダがありません。接続と実行環境を確認してください。"
        }
        return nil
    }
}
