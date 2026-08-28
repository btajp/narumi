import Foundation

/// Reads through the same signed executable that creates the Keychain item.
/// The helper URL must come from the app bundle or an explicit local dev setting,
/// never a server response. The helper receives only an anonymous stdin pipe.
public struct KeychainHelperSecretReader: KeychainSecretReading {
    private let helperURL: URL
    private let executor: any KeychainHelperExecuting

    public init(helperURL: URL) {
        self.init(helperURL: helperURL, executor: KeychainHelperProcessExecutor())
    }

    init(helperURL: URL, executor: any KeychainHelperExecuting) {
        self.helperURL = helperURL
        self.executor = executor
    }

    public func get(account: String) throws -> String? {
        guard KeychainSecretStore.validAccount(account) else {
            throw KeychainSecretError.invalidAccount
        }
        guard helperURL.isFileURL else { throw KeychainSecretError.keychainUnavailable }
        do {
            let request = try JSONSerialization.data(
                withJSONObject: ["operation": "get", "account": account], options: [.sortedKeys])
            let result = try executor.run(executable: helperURL, input: request)
            guard result.exitStatus == 0,
                  result.output.count <= KeychainHelperProtocol.maximumResponseBytes
            else { throw KeychainSecretError.keychainUnavailable }
            var parser = KeychainJSONFields(data: result.output)
            let fields = try parser.parse()
            guard fields["ok"] == .bool(true), Set(fields.keys) == ["ok", "value"] else {
                throw KeychainSecretError.keychainUnavailable
            }
            if fields["value"] == .null { return nil }
            guard let value = fields["value"]?.string, KeychainSecretStore.validValue(value) else {
                throw KeychainSecretError.invalidStoredSecret
            }
            return value
        } catch let error as KeychainSecretError {
            throw error
        } catch {
            throw KeychainSecretError.keychainUnavailable
        }
    }
}

protocol KeychainHelperExecuting: Sendable {
    func run(executable: URL, input: Data) throws -> KeychainHelperResult
}
