import Foundation

public struct MinutesEnsembleGenerator: Codable, Equatable, Sendable, Identifiable {
    public let id: String
    public var label: String
    public var selection: MinutesModelSelection

    enum CodingKeys: String, CodingKey, CaseIterable { case id, label, selection }

    public init(id: String, label: String, selection: MinutesModelSelection) {
        self.id = id
        self.label = label
        self.selection = selection
    }

    public init(from decoder: Decoder) throws {
        try requireContractKeys(
            decoder, allowed: Set(CodingKeys.allCases.map(\.rawValue)),
            required: Set(CodingKeys.allCases.map(\.rawValue)))
        let container = try decoder.container(keyedBy: CodingKeys.self)
        id = try container.decode(String.self, forKey: .id)
        label = try container.decode(String.self, forKey: .label)
        selection = try container.decode(MinutesModelSelection.self, forKey: .selection)
        guard isWellFormed else {
            throw DecodingError.dataCorruptedError(
                forKey: .id, in: container, debugDescription: "Invalid minutes ensemble generator")
        }
    }

    public func encode(to encoder: Encoder) throws {
        guard isWellFormed else {
            throw EncodingError.invalidValue(self, .init(
                codingPath: encoder.codingPath, debugDescription: "Invalid minutes ensemble generator"))
        }
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(id, forKey: .id); try container.encode(label, forKey: .label)
        try container.encode(selection, forKey: .selection)
    }

    public var isWellFormed: Bool {
        Self.isValidID(id) && (1...80).contains(label.unicodeScalars.count)
            && label.range(of: #"\S"#, options: .regularExpression) != nil
            && selection.isWellFormed
    }

    public static func isValidID(_ value: String) -> Bool {
        ProcessingIdentifier.matches(value, prefix: "gen-", hexCount: 32)
    }

    public static func newID(uuid: UUID = UUID()) -> String {
        "gen-" + uuid.uuidString.replacingOccurrences(of: "-", with: "").lowercased()
    }
}

public struct MinutesEnsembleSelection: Codable, Equatable, Sendable {
    public var generators: [MinutesEnsembleGenerator]
    public var synthesizer: MinutesModelSelection

    enum CodingKeys: String, CodingKey, CaseIterable { case generators, synthesizer }

    public init(generators: [MinutesEnsembleGenerator], synthesizer: MinutesModelSelection) {
        self.generators = generators
        self.synthesizer = synthesizer
    }

    public init(from decoder: Decoder) throws {
        try requireContractKeys(
            decoder, allowed: Set(CodingKeys.allCases.map(\.rawValue)),
            required: Set(CodingKeys.allCases.map(\.rawValue)))
        let container = try decoder.container(keyedBy: CodingKeys.self)
        generators = try container.decode([MinutesEnsembleGenerator].self, forKey: .generators)
        synthesizer = try container.decode(MinutesModelSelection.self, forKey: .synthesizer)
        guard isWellFormed else {
            throw DecodingError.dataCorruptedError(
                forKey: .generators, in: container, debugDescription: "Invalid minutes ensemble")
        }
    }

    public func encode(to encoder: Encoder) throws {
        guard isWellFormed else {
            throw EncodingError.invalidValue(self, .init(
                codingPath: encoder.codingPath, debugDescription: "Invalid minutes ensemble"))
        }
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(generators, forKey: .generators)
        try container.encode(synthesizer, forKey: .synthesizer)
    }

    public var isWellFormed: Bool {
        guard (2...4).contains(generators.count), synthesizer.isWellFormed,
            generators.allSatisfy(\.isWellFormed) else { return false }
        return Set(generators.map(\.id)).count == generators.count
    }
}
