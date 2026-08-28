import Foundation
import Security

@testable import NarumiMenuBarCore

/// Per-test memory only; no Security function or user's Keychain is called.
final class KeychainTestBackend: KeychainItemBackend, @unchecked Sendable {
    struct Call {
        let operation: String
        let query: [String: Any]
        let attributes: [String: Any]
    }

    var items: [String: Data] = [:]
    var otherItems: [String: Data] = [:]
    var calls: [Call] = []
    var defaultKeychainRequests = 0
    var defaultKeychainStatus: OSStatus = errSecSuccess
    var readStatus: OSStatus?
    var readResult: CFTypeRef?
    var updateStatuses: [OSStatus] = []
    var addStatus: OSStatus?
    var deleteStatus: OSStatus?

    func defaultKeychain() -> (OSStatus, CFTypeRef?) {
        defaultKeychainRequests += 1
        return (defaultKeychainStatus, "fixture-keychain" as NSString)
    }

    func copyMatching(_ query: [String: Any]) -> (OSStatus, CFTypeRef?) {
        calls.append(Call(operation: "get", query: query, attributes: [:]))
        if let readStatus { return (readStatus, readResult) }
        let value = (searchesDefault(query) ? items[account(query)] : nil)
            ?? (searchesOther(query) ? otherItems[account(query)] : nil)
        guard let value else { return (errSecItemNotFound, nil) }
        return (errSecSuccess, value as NSData)
    }

    func add(_ attributes: [String: Any]) -> OSStatus {
        calls.append(Call(operation: "add", query: attributes, attributes: [:]))
        if let addStatus { return addStatus }
        let account = account(attributes)
        guard items[account] == nil else { return errSecDuplicateItem }
        items[account] = attributes[kSecValueData as String] as? Data
        return errSecSuccess
    }

    func update(_ query: [String: Any], attributes: [String: Any]) -> OSStatus {
        calls.append(Call(operation: "update", query: query, attributes: attributes))
        if !updateStatuses.isEmpty {
            let status = updateStatuses.removeFirst()
            if status != errSecSuccess { return status }
            items[account(query)] = attributes[kSecValueData as String] as? Data
            return errSecSuccess
        }
        var updated = false
        if searchesDefault(query), items[account(query)] != nil {
            items[account(query)] = attributes[kSecValueData as String] as? Data
            updated = true
        }
        if searchesOther(query), otherItems[account(query)] != nil {
            otherItems[account(query)] = attributes[kSecValueData as String] as? Data
            updated = true
        }
        return updated ? errSecSuccess : errSecItemNotFound
    }

    func delete(_ query: [String: Any]) -> OSStatus {
        calls.append(Call(operation: "delete", query: query, attributes: [:]))
        if let deleteStatus { return deleteStatus }
        let removedDefault = searchesDefault(query) && items.removeValue(forKey: account(query)) != nil
        let removedOther = searchesOther(query) && otherItems.removeValue(forKey: account(query)) != nil
        return removedDefault || removedOther ? errSecSuccess : errSecItemNotFound
    }

    private func account(_ query: [String: Any]) -> String {
        query[kSecAttrAccount as String] as? String ?? "invalid-test-query"
    }

    private func searchesDefault(_ query: [String: Any]) -> Bool {
        (query[kSecMatchSearchList as String] as? [String])?.contains("fixture-keychain") ?? true
    }

    private func searchesOther(_ query: [String: Any]) -> Bool {
        (query[kSecMatchSearchList as String] as? [String])?.contains("fixture-backup-keychain") ?? true
    }
}
