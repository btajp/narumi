import Foundation

struct ContractFieldKey: CodingKey {
    let stringValue: String
    var intValue: Int? { nil }
    init?(stringValue: String) { self.stringValue = stringValue }
    init?(intValue: Int) { return nil }
}

func requireContractKeys(
    _ decoder: Decoder, allowed: Set<String>, required: Set<String>
) throws {
    let container = try decoder.container(keyedBy: ContractFieldKey.self)
    let present = Set(container.allKeys.map(\.stringValue))
    guard present.isSubset(of: allowed), required.isSubset(of: present) else {
        throw DecodingError.dataCorrupted(
            .init(codingPath: decoder.codingPath, debugDescription: "Unexpected or missing contract fields"))
    }
}

enum ProcessingIdentifier {
    static func matches(_ value: String, prefix: String, hexCount: Int) -> Bool {
        let bytes = Array(value.utf8)
        let prefixBytes = Array(prefix.utf8)
        guard bytes.count == prefixBytes.count + hexCount,
            bytes.prefix(prefixBytes.count).elementsEqual(prefixBytes) else { return false }
        return bytes.dropFirst(prefixBytes.count).allSatisfy {
            (48...57).contains($0) || (97...102).contains($0)
        }
    }

    static func run(_ value: String) -> Bool { matches(value, prefix: "run-", hexCount: 32) }
    static func slot(_ value: String) -> Bool { matches(value, prefix: "slot-", hexCount: 64) }
    static func node(_ value: String) -> Bool { matches(value, prefix: "node-", hexCount: 64) }
    static func call(_ value: String) -> Bool { matches(value, prefix: "call-", hexCount: 64) }
    static func attempt(_ value: String) -> Bool { matches(value, prefix: "attempt-", hexCount: 32) }
    static func artifact(_ value: String) -> Bool { matches(value, prefix: "artifact-", hexCount: 32) }
    static func sha256(_ value: String) -> Bool { matches(value, prefix: "", hexCount: 64) }
}
