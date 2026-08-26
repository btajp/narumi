import Foundation

/// Minimal ordered JSON model with a deterministic compact serializer.
///
/// Foundation's `JSONEncoder` does not guarantee key order; the recorder protocol is parsed by
/// another process, so every line printed on stdout is built from this type instead.
public indirect enum JSONValue: Equatable, Sendable {
    case string(String)
    case number(Double)
    case integer(Int)
    case bool(Bool)
    case null
    case array([JSONValue])
    case object([JSONMember])

    /// Build an object while keeping the literal order of the pairs.
    public static func obj(_ pairs: KeyValuePairs<String, JSONValue>) -> JSONValue {
        .object(pairs.map { JSONMember(key: $0.key, value: $0.value) })
    }

    /// Compact single-line serialization (no whitespace, UTF-8 passthrough).
    public func serialized() -> String {
        var out = ""
        write(into: &out)
        return out
    }

    private func write(into out: inout String) {
        switch self {
        case .string(let value):
            JSONValue.writeString(value, into: &out)
        case .number(let value):
            JSONValue.writeNumber(value, into: &out)
        case .integer(let value):
            out.append(String(value))
        case .bool(let value):
            out.append(value ? "true" : "false")
        case .null:
            out.append("null")
        case .array(let items):
            out.append("[")
            for (index, item) in items.enumerated() {
                if index > 0 { out.append(",") }
                item.write(into: &out)
            }
            out.append("]")
        case .object(let members):
            out.append("{")
            for (index, member) in members.enumerated() {
                if index > 0 { out.append(",") }
                JSONValue.writeString(member.key, into: &out)
                out.append(":")
                member.value.write(into: &out)
            }
            out.append("}")
        }
    }

    private static func writeNumber(_ value: Double, into out: inout String) {
        guard value.isFinite else {
            out.append("null")
            return
        }
        out.append(value.description)
    }

    private static func writeString(_ value: String, into out: inout String) {
        out.append("\"")
        for scalar in value.unicodeScalars {
            switch scalar {
            case "\"": out.append("\\\"")
            case "\\": out.append("\\\\")
            case "\n": out.append("\\n")
            case "\r": out.append("\\r")
            case "\t": out.append("\\t")
            case "\u{08}": out.append("\\b")
            case "\u{0C}": out.append("\\f")
            default:
                if scalar.value < 0x20 {
                    out.append(String(format: "\\u%04x", scalar.value))
                } else {
                    out.unicodeScalars.append(scalar)
                }
            }
        }
        out.append("\"")
    }
}

public struct JSONMember: Equatable, Sendable {
    public let key: String
    public let value: JSONValue

    public init(key: String, value: JSONValue) {
        self.key = key
        self.value = value
    }
}

extension Double {
    /// Round to milliseconds so serialized durations do not carry float noise.
    public var roundedToMilliseconds: Double {
        (self * 1000).rounded() / 1000
    }
}
