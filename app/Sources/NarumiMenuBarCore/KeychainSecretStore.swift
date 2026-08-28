import Foundation
import Security

/// A missing item is distinct from an unavailable or locked Keychain.
public protocol KeychainSecretReading: Sendable {
    func get(account: String) throws -> String?
}

public protocol KeychainSecretStoring: KeychainSecretReading {
    func set(value: String, account: String) throws
    func delete(account: String) throws
}

public enum KeychainSecretError: String, Error, Sendable {
    case invalidAccount = "invalid_account"
    case invalidSecret = "invalid_secret"
    case invalidStoredSecret = "keychain_item_invalid"
    case keychainUnavailable = "keychain_unavailable"
}

/// Native storage for the narumi-keychain helper. Other processes use
/// KeychainHelperSecretReader so the same executable retains the Keychain ACL.
public struct KeychainSecretStore: KeychainSecretStoring {
    public static let service = "jp.btajp.narumi.secrets.v1"
    public static let maximumValueBytes = 16 * 1024
    private let backend: any KeychainItemBackend

    public init() {
        self.backend = SystemKeychainItemBackend()
    }

    init(backend: any KeychainItemBackend) {
        self.backend = backend
    }

    public func get(account: String) throws -> String? {
        var query = try itemQuery(account: account).query
        query[kSecReturnData as String] = true
        query[kSecMatchLimit as String] = kSecMatchLimitOne
        let (status, result) = backend.copyMatching(query)
        if status == errSecItemNotFound { return nil }
        guard status == errSecSuccess else { throw KeychainSecretError.keychainUnavailable }
        guard let data = result as? Data,
              data.count <= Self.maximumValueBytes,
              let value = String(data: data, encoding: .utf8),
              Self.validValue(value)
        else { throw KeychainSecretError.invalidStoredSecret }
        return value
    }

    public func set(value: String, account: String) throws {
        guard Self.validAccount(account) else { throw KeychainSecretError.invalidAccount }
        guard Self.validValue(value) else { throw KeychainSecretError.invalidSecret }
        let scoped = try itemQuery(account: account)
        let query = scoped.query
        let attributes = [kSecValueData as String: Data(value.utf8)]
        let updateStatus = backend.update(query, attributes: attributes)
        if updateStatus == errSecSuccess { return }
        guard updateStatus == errSecItemNotFound else {
            throw KeychainSecretError.keychainUnavailable
        }

        var item = query
        item.removeValue(forKey: kSecMatchSearchList as String)
        item[kSecUseKeychain as String] = scoped.keychain
        item[kSecValueData as String] = attributes[kSecValueData as String]
        let addStatus = backend.add(item)
        if addStatus == errSecSuccess { return }
        // Another helper may have inserted this exact item between update and add.
        if addStatus == errSecDuplicateItem,
           backend.update(query, attributes: attributes) == errSecSuccess { return }
        throw KeychainSecretError.keychainUnavailable
    }

    public func delete(account: String) throws {
        let status = backend.delete(try itemQuery(account: account).query)
        guard status == errSecSuccess || status == errSecItemNotFound else {
            throw KeychainSecretError.keychainUnavailable
        }
    }

    static func validAccount(_ account: String) -> Bool {
        let bytes = account.utf8
        return (1...256).contains(bytes.count) && bytes.allSatisfy {
            (65...90).contains($0) || (97...122).contains($0) || (48...57).contains($0)
                || [45, 46, 58, 95].contains($0)
        }
    }

    static func validValue(_ value: String) -> Bool {
        !value.isEmpty && value.utf8.count <= maximumValueBytes
            && !value.unicodeScalars.contains { CharacterSet.controlCharacters.contains($0) }
    }

    private func itemQuery(account: String) throws -> (query: [String: Any], keychain: CFTypeRef) {
        guard Self.validAccount(account) else { throw KeychainSecretError.invalidAccount }
        let (status, keychain) = backend.defaultKeychain()
        guard status == errSecSuccess, let keychain else {
            throw KeychainSecretError.keychainUnavailable
        }
        // Read, update, delete and a raced insert must target the same Keychain.
        // Service/account alone also match copies in other user search-list entries.
        return ([
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: Self.service,
            kSecAttrAccount as String: account,
            kSecMatchSearchList as String: [keychain],
            kSecAttrSynchronizable as String: false,
            // This CLI has no provisioning profile or shared access-group entitlement.
            // Keep all access in the signed helper and never broaden the default ACL.
            kSecUseDataProtectionKeychain as String: false,
        ], keychain)
    }
}

protocol KeychainItemBackend: Sendable {
    func defaultKeychain() -> (OSStatus, CFTypeRef?)
    func copyMatching(_ query: [String: Any]) -> (OSStatus, CFTypeRef?)
    func add(_ attributes: [String: Any]) -> OSStatus
    func update(_ query: [String: Any], attributes: [String: Any]) -> OSStatus
    func delete(_ query: [String: Any]) -> OSStatus
}

private struct SystemKeychainItemBackend: KeychainItemBackend {
    func defaultKeychain() -> (OSStatus, CFTypeRef?) {
        guard disableInteraction() else { return (errSecInteractionNotAllowed, nil) }
        var keychain: SecKeychain?
        let status = SecKeychainCopyDefault(&keychain)
        return (status, keychain)
    }

    func copyMatching(_ query: [String: Any]) -> (OSStatus, CFTypeRef?) {
        guard disableInteraction() else { return (errSecInteractionNotAllowed, nil) }
        var result: CFTypeRef?
        let status = SecItemCopyMatching(query as CFDictionary, &result)
        return (status, result)
    }

    func add(_ attributes: [String: Any]) -> OSStatus {
        guard disableInteraction() else { return errSecInteractionNotAllowed }
        return SecItemAdd(attributes as CFDictionary, nil)
    }

    func update(_ query: [String: Any], attributes: [String: Any]) -> OSStatus {
        guard disableInteraction() else { return errSecInteractionNotAllowed }
        return SecItemUpdate(query as CFDictionary, attributes as CFDictionary)
    }

    func delete(_ query: [String: Any]) -> OSStatus {
        guard disableInteraction() else { return errSecInteractionNotAllowed }
        return SecItemDelete(query as CFDictionary)
    }

    private func disableInteraction() -> Bool {
        // The legacy Keychain's process-wide switch is intentional in the short-lived
        // helper: locked items or ACL changes fail instead of opening a hidden prompt.
        SecKeychainSetUserInteractionAllowed(false) == errSecSuccess
    }
}
