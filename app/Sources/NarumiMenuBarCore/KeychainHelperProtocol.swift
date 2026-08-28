import Foundation

public struct KeychainHelperResult: Sendable {
    public let output: Data
    public let exitStatus: Int32

    public static func invalidRequest() -> Self {
        failure("invalid_request")
    }

    public static func unavailable() -> Self {
        failure(KeychainSecretError.keychainUnavailable.rawValue)
    }

    fileprivate static func success(value: String? = nil, includeValue: Bool = false) -> Self {
        var fields: [String: Any] = ["ok": true]
        if includeValue { fields["value"] = value.map { $0 as Any } ?? NSNull() }
        return encoded(fields, exitStatus: 0)
    }

    fileprivate static func failure(_ code: String) -> Self {
        encoded(["ok": false, "error": code], exitStatus: 1)
    }

    private static func encoded(_ fields: [String: Any], exitStatus: Int32) -> Self {
        guard var data = try? JSONSerialization.data(withJSONObject: fields, options: [.sortedKeys]) else {
            return Self(output: Data("{\"ok\":false,\"error\":\"keychain_unavailable\"}\n".utf8), exitStatus: 1)
        }
        data.append(10)
        return Self(output: data, exitStatus: exitStatus)
    }
}

public enum KeychainHelperProtocol {
    public static let maximumRequestBytes = 128 * 1024
    public static let maximumResponseBytes = 128 * 1024

    /// One request per process. No command line arguments or inherited secrets.
    public static func handle(
        _ data: Data, argumentCount: Int = 1, store: any KeychainSecretStoring
    ) -> KeychainHelperResult {
        guard argumentCount == 1, !data.isEmpty, data.count <= maximumRequestBytes else {
            return .invalidRequest()
        }
        let request: Request
        do {
            request = try Request(data: data)
        } catch {
            return .invalidRequest()
        }
        do {
            switch request.operation {
            case "get":
                return .success(value: try store.get(account: request.account), includeValue: true)
            case "set":
                guard let value = request.value else { return .invalidRequest() }
                try store.set(value: value, account: request.account)
                return .success()
            case "delete":
                try store.delete(account: request.account)
                return .success()
            default:
                return .invalidRequest()
            }
        } catch let error as KeychainSecretError {
            return .failure(error.rawValue)
        } catch {
            // Backend and decoding exceptions must never include the request or value.
            return .unavailable()
        }
    }

    private struct Request {
        let operation: String
        let account: String
        let value: String?

        init(data: Data) throws {
            var parser = KeychainJSONFields(data: data)
            let fields = try parser.parse()
            guard let operation = fields["operation"]?.string,
                  ["get", "set", "delete"].contains(operation),
                  let account = fields["account"]?.string,
                  KeychainSecretStore.validAccount(account)
            else { throw KeychainJSONFields.InvalidJSON.invalid }
            let expected: Set<String> = operation == "set"
                ? ["operation", "account", "value"] : ["operation", "account"]
            guard Set(fields.keys) == expected else { throw KeychainJSONFields.InvalidJSON.invalid }
            let value = fields["value"]?.string
            if operation == "set" {
                guard let value, KeychainSecretStore.validValue(value) else {
                    throw KeychainJSONFields.InvalidJSON.invalid
                }
            }
            self.operation = operation
            self.account = account
            self.value = value
        }
    }
}
