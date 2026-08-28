import Foundation

/// The helper accepts only a flat object of JSON scalars. Parsing keys before
/// constructing a dictionary rejects duplicate keys, including escaped spellings.
enum KeychainJSONValue: Equatable {
    case string(String)
    case bool(Bool)
    case null

    var string: String? {
        if case .string(let value) = self { return value }
        return nil
    }
}

struct KeychainJSONFields {
    enum InvalidJSON: Error { case invalid }
    private let bytes: [UInt8]
    private var cursor = 0

    init(data: Data) {
        self.bytes = Array(data)
    }

    mutating func parse() throws -> [String: KeychainJSONValue] {
        try consume(123)
        var fields: [String: KeychainJSONValue] = [:]
        skipWhitespace()
        if !peek(125) {
            while true {
                let key = try string()
                guard fields[key] == nil else { throw InvalidJSON.invalid }
                try consume(58)
                fields[key] = try value()
                skipWhitespace()
                if peek(125) { break }
                try consume(44)
            }
        }
        try consume(125)
        skipWhitespace()
        guard cursor == bytes.count else { throw InvalidJSON.invalid }
        return fields
    }

    private mutating func value() throws -> KeychainJSONValue {
        skipWhitespace()
        if peek(34) { return .string(try string()) }
        for (literal, value): (String, KeychainJSONValue) in [
            ("true", .bool(true)), ("false", .bool(false)), ("null", .null),
        ] {
            let sequence = Array(literal.utf8)
            if bytes[cursor...].starts(with: sequence) {
                cursor += sequence.count
                return value
            }
        }
        throw InvalidJSON.invalid
    }

    private mutating func string() throws -> String {
        skipWhitespace()
        let start = cursor
        try consume(34)
        while cursor < bytes.count {
            if peek(34) {
                cursor += 1
                return try JSONDecoder().decode(String.self, from: Data(bytes[start..<cursor]))
            }
            if peek(92) {
                cursor += 1
                guard cursor < bytes.count else { throw InvalidJSON.invalid }
            }
            cursor += 1
        }
        throw InvalidJSON.invalid
    }

    private func peek(_ byte: UInt8) -> Bool {
        cursor < bytes.count && bytes[cursor] == byte
    }

    private mutating func consume(_ byte: UInt8) throws {
        skipWhitespace()
        guard peek(byte) else { throw InvalidJSON.invalid }
        cursor += 1
    }

    private mutating func skipWhitespace() {
        while cursor < bytes.count && [9, 10, 13, 32].contains(bytes[cursor]) {
            cursor += 1
        }
    }
}
