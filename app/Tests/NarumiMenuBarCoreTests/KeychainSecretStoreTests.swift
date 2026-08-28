import Security
import XCTest

@testable import NarumiMenuBarCore

final class KeychainSecretStoreTests: XCTestCase {
    private let account = "providers:fixture-root:fixture-connection"

    func testCreateReadUpdateAndDeleteAreScopedToOneNonSyncedServiceItem() throws {
        let backend = KeychainTestBackend()
        let store = KeychainSecretStore(backend: backend)
        backend.items["transport:fixture-root:other"] = Data("other-fixture".utf8)
        XCTAssertNil(try store.get(account: account))
        try store.set(value: "fixture-first", account: account)
        XCTAssertEqual(try store.get(account: account), "fixture-first")
        try store.set(value: "fixture-updated", account: account)
        XCTAssertEqual(try store.get(account: account), "fixture-updated")
        try store.delete(account: account)
        try store.delete(account: account)
        XCTAssertNil(try store.get(account: account))
        XCTAssertEqual(backend.items, ["transport:fixture-root:other": Data("other-fixture".utf8)])
        XCTAssertEqual(backend.calls.filter { $0.operation == "add" }.count, 1)

        for call in backend.calls {
            XCTAssertEqual(call.query[kSecClass as String] as? String, kSecClassGenericPassword as String)
            XCTAssertEqual(call.query[kSecAttrService as String] as? String, KeychainSecretStore.service)
            XCTAssertEqual(call.query[kSecAttrAccount as String] as? String, account)
            XCTAssertEqual(call.query[kSecAttrSynchronizable as String] as? Bool, false)
            XCTAssertEqual(call.query[kSecUseDataProtectionKeychain as String] as? Bool, false)
            XCTAssertNil(call.query[kSecAttrAccess as String], "never broaden another item's ACL")
            if call.operation == "add" {
                XCTAssertNil(call.query[kSecMatchSearchList as String])
                XCTAssertEqual(call.query[kSecUseKeychain as String] as? String, "fixture-keychain")
            } else {
                XCTAssertEqual(call.query[kSecMatchSearchList as String] as? [String], ["fixture-keychain"])
            }
            if call.operation == "get" {
                XCTAssertEqual(call.query[kSecMatchLimit as String] as? String, kSecMatchLimitOne as String)
                XCTAssertEqual(call.query[kSecReturnData as String] as? Bool, true)
            }
            if call.operation == "update" {
                XCTAssertEqual(Set(call.attributes.keys), [kSecValueData as String])
                XCTAssertNil(call.query[kSecValueData as String])
            }
        }
    }

    func testIdenticalAccountsInAnotherKeychainAreNeverReadUpdatedOrDeleted() throws {
        let backend = KeychainTestBackend()
        let store = KeychainSecretStore(backend: backend)
        backend.otherItems[account] = Data("fixture-backup-value".utf8)
        XCTAssertNil(try store.get(account: account))
        try store.set(value: "fixture-active-value", account: account)
        XCTAssertEqual(try store.get(account: account), "fixture-active-value")
        try store.set(value: "fixture-updated-value", account: account)
        try store.delete(account: account)
        XCTAssertNil(try store.get(account: account))
        XCTAssertEqual(backend.otherItems[account], Data("fixture-backup-value".utf8))
    }

    func testMissingDefaultKeychainDoesNotFallBackToTheSearchList() {
        let backend = KeychainTestBackend()
        backend.defaultKeychainStatus = errSecNoDefaultKeychain
        backend.otherItems[account] = Data("fixture-backup-value".utf8)
        let store = KeychainSecretStore(backend: backend)
        XCTAssertThrowsError(try store.get(account: account)) {
            XCTAssertEqual($0 as? KeychainSecretError, .keychainUnavailable)
        }
        XCTAssertTrue(backend.calls.isEmpty)
    }

    func testDuplicateDuringInsertRaceRetriesOnlyTheExactUpdate() throws {
        let backend = KeychainTestBackend()
        backend.updateStatuses = [errSecItemNotFound, errSecSuccess]
        backend.addStatus = errSecDuplicateItem
        try KeychainSecretStore(backend: backend).set(value: "fixture-value", account: account)
        XCTAssertEqual(backend.calls.map(\.operation), ["update", "add", "update"])
        XCTAssertEqual(backend.items[account], Data("fixture-value".utf8))
    }

    func testDeniedUpdateNeverDeletesOrRecreatesTheExistingSecret() {
        let backend = KeychainTestBackend()
        backend.items[account] = Data("fixture-previous".utf8)
        backend.updateStatuses = [errSecAuthFailed]
        XCTAssertThrowsError(try KeychainSecretStore(backend: backend).set(value: "fixture-new", account: account)) {
            XCTAssertEqual($0 as? KeychainSecretError, .keychainUnavailable)
        }
        XCTAssertEqual(backend.calls.map(\.operation), ["update"])
        XCTAssertEqual(backend.items[account], Data("fixture-previous".utf8))
    }

    func testAddAndRacedUpdateFailuresDoNotRetryOrFallBack() {
        for statuses in [[errSecItemNotFound], [errSecItemNotFound, errSecAuthFailed]] {
            let backend = KeychainTestBackend()
            backend.updateStatuses = statuses
            backend.addStatus = statuses.count == 1 ? errSecAuthFailed : errSecDuplicateItem
            XCTAssertThrowsError(try KeychainSecretStore(backend: backend).set(value: "fixture-value", account: account)) {
                XCTAssertEqual($0 as? KeychainSecretError, .keychainUnavailable)
            }
            XCTAssertEqual(backend.calls.count, statuses.count + 1)
            XCTAssertFalse(backend.calls.contains { $0.operation == "delete" })
        }
    }

    func testLockedAndDeniedReadsAreNotReportedAsMissingItems() {
        for status in [errSecInteractionNotAllowed, errSecAuthFailed, errSecNotAvailable] {
            let backend = KeychainTestBackend()
            backend.readStatus = status
            XCTAssertThrowsError(try KeychainSecretStore(backend: backend).get(account: account)) {
                XCTAssertEqual($0 as? KeychainSecretError, .keychainUnavailable)
            }
        }
    }

    func testDeleteFailureIsSafeAndKeepsTheExistingItem() {
        let backend = KeychainTestBackend()
        backend.items[account] = Data("fixture-previous".utf8)
        backend.deleteStatus = errSecInteractionNotAllowed
        XCTAssertThrowsError(try KeychainSecretStore(backend: backend).delete(account: account)) {
            XCTAssertEqual($0 as? KeychainSecretError, .keychainUnavailable)
        }
        XCTAssertEqual(backend.items[account], Data("fixture-previous".utf8))
    }

    func testInvalidStoredResultsAreNeverReturned() {
        let invalid: [CFTypeRef?] = [
            nil, "fixture-wrong-type" as NSString, Data([0xff]) as NSData, Data() as NSData,
            Data("fixture\nvalue".utf8) as NSData,
            Data(repeating: 65, count: KeychainSecretStore.maximumValueBytes + 1) as NSData,
        ]
        for value in invalid {
            let backend = KeychainTestBackend()
            backend.readStatus = errSecSuccess
            backend.readResult = value
            XCTAssertThrowsError(try KeychainSecretStore(backend: backend).get(account: account)) {
                XCTAssertEqual($0 as? KeychainSecretError, .invalidStoredSecret)
            }
        }
    }

    func testAccountValidationPrecedesEveryKeychainOperation() {
        for account in ["", "a/b", "a b", "a\n", "a\0", "日本語", "a\\b", String(repeating: "a", count: 257)] {
            let backend = KeychainTestBackend()
            let store = KeychainSecretStore(backend: backend)
            for operation: () throws -> Void in [
                { _ = try store.get(account: account) },
                { try store.set(value: "fixture-value", account: account) },
                { try store.delete(account: account) },
            ] {
                XCTAssertThrowsError(try operation()) {
                    XCTAssertEqual($0 as? KeychainSecretError, .invalidAccount)
                }
            }
            XCTAssertTrue(backend.calls.isEmpty)
            XCTAssertEqual(backend.defaultKeychainRequests, 0)
        }
    }

    func testValueValidationUsesUTF8ByteLimitsAndRejectsControlCharacters() throws {
        let backend = KeychainTestBackend()
        let store = KeychainSecretStore(backend: backend)
        let invalid = ["", "fixture\0", "fixture\r\n", "fixture\t", "fixture\u{7f}",
                       String(repeating: "a", count: KeychainSecretStore.maximumValueBytes + 1),
                       String(repeating: "あ", count: KeychainSecretStore.maximumValueBytes / 3 + 1)]
        for value in invalid {
            XCTAssertThrowsError(try store.set(value: value, account: account)) {
                XCTAssertEqual($0 as? KeychainSecretError, .invalidSecret)
            }
        }
        XCTAssertTrue(backend.calls.isEmpty)
        XCTAssertEqual(backend.defaultKeychainRequests, 0)
        let boundary = String(repeating: "a", count: KeychainSecretStore.maximumValueBytes)
        let boundaryAccount = String(repeating: "a", count: 256)
        try store.set(value: boundary, account: boundaryAccount)
        XCTAssertEqual(try store.get(account: boundaryAccount), boundary)
        XCTAssertTrue(KeychainSecretStore.validAccount("A-z_0:9.fixture"))
    }
}
