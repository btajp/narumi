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
