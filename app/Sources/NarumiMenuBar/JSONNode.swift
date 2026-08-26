import Foundation

/// Sendable JSON tree used for JSON-RPC payloads (Foundation's `Any` graphs are not Sendable).
indirect enum JSONNode: Equatable, Sendable {
    case string(String)
    case number(Double)
    case bool(Bool)
    case null
    case array([JSONNode])
    case object([String: JSONNode])

    subscript(key: String) -> JSONNode? {
        if case .object(let members) = self {
            return members[key]
        }
        return nil
    }

    var stringValue: String? {
        if case .string(let value) = self {
            return value
        }
        return nil
    }

    var boolValue: Bool? {
        if case .bool(let value) = self {
            return value
        }
        return nil
    }

    var isNull: Bool {
        if case .null = self {
            return true
        }
        return false
    }

    // MARK: Foundation bridging

    static func from(_ any: Any) throws -> JSONNode {
        switch any {
        case let value as String:
            return .string(value)
        case let value as NSNumber:
            if CFGetTypeID(value) == CFBooleanGetTypeID() {
                return .bool(value.boolValue)
            }
            return .number(value.doubleValue)
        case is NSNull:
            return .null
        case let value as [Any]:
            return .array(try value.map(from))
        case let value as [String: Any]:
            var members: [String: JSONNode] = [:]
            for (key, element) in value {
                members[key] = try from(element)
            }
            return .object(members)
        default:
            throw MCPClientError.protocolError("unsupported JSON value: \(type(of: any))")
        }
    }

    func toFoundation() -> Any {
        switch self {
        case .string(let value): return value
        case .number(let value): return value
        case .bool(let value): return value
        case .null: return NSNull()
        case .array(let items): return items.map { $0.toFoundation() }
        case .object(let members): return members.mapValues { $0.toFoundation() }
        }
    }

    static func parse(_ data: Data) throws -> JSONNode {
        let object: Any
        do {
            object = try JSONSerialization.jsonObject(with: data, options: [.fragmentsAllowed])
        } catch {
            throw MCPClientError.protocolError("invalid JSON from server: \(error.localizedDescription)")
        }
        return try from(object)
    }

    func serialized() throws -> Data {
        do {
            return try JSONSerialization.data(withJSONObject: toFoundation(), options: [])
        } catch {
            throw MCPClientError.protocolError("cannot encode request: \(error.localizedDescription)")
        }
    }

    /// Human-readable rendering for alerts.
    func pretty() -> String {
        guard
            let data = try? JSONSerialization.data(
                withJSONObject: toFoundation(), options: [.prettyPrinted, .sortedKeys, .fragmentsAllowed]),
            let text = String(data: data, encoding: .utf8)
        else {
            return String(describing: self)
        }
        return text
    }
}
