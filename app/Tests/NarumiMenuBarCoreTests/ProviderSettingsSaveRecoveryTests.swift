import Foundation
import XCTest
@testable import NarumiMenuBarCore

@MainActor
final class ProviderSettingsSaveRecoveryTests: XCTestCase {
    private let key = "not-a-real-recovery-key"

    func testUndeliveredCreateCanBeExplicitlyRetriedWithSameRequestAndReenteredKey() async throws {
        let client = FakeProviderSettingsClient(connections: [], scenario: .init(saveFailure: ProviderSettingsFailure(.transport)))
        let store = ProviderSettingsStore(client: client)
        await store.load()
        store.editor.displayName = "Undelivered create"
        store.editor.apiKey = key
        await store.save()
        let summary = try XCTUnwrap(store.saveRecoverySummary)
        XCTAssertTrue(summary.requiresAPIKeyReentry)
        XCTAssertFalse(String(reflecting: summary).contains(key))
        XCTAssertFalse(String(reflecting: store.pendingSave).contains(key))
        XCTAssertEqual(store.editor.apiKey, "")
        await store.load(discardEdits: true)
        XCTAssertFalse(store.canAddConnection)
        XCTAssertFalse(store.canAdoptSavedConnectionForRecovery)
        XCTAssertFalse(store.canDiscardMissingConnectionChange)
        XCTAssertTrue(store.canRetryPendingSave)
        await client.configure(.init())
        await store.retryPendingSave(apiKey: key)
        XCTAssertFalse(store.saveNeedsReconciliation)
        XCTAssertEqual(store.selectedConnection?.displayName, "Undelivered create")
        XCTAssertTrue(store.selectedConnection?.credentialPresent ?? false)
        XCTAssertEqual(store.editor.apiKey, "")
        let requests = await client.saveRequests
        let connections = await client.connections
        let mutations = await client.saveMutationCount
        XCTAssertEqual(requests.count, 2)
        XCTAssertEqual(requests[0], requests[1])
        XCTAssertEqual(requests[1].requestID, summary.requestID)
        XCTAssertEqual(connections.count, 1)
        XCTAssertEqual(mutations, 1)
    }

    func testMissingRetryKeyDoesNotSendOrClearPendingSave() async {
        let client = FakeProviderSettingsClient(connections: [], scenario: .init(saveFailure: ProviderSettingsFailure(.transport)))
        let store = ProviderSettingsStore(client: client)
        await store.load()
        store.editor.displayName = "Needs reentry"
        store.editor.apiKey = key
        await store.save()
        await store.retryPendingSave()
        XCTAssertTrue(store.saveNeedsReconciliation)
        let requests = await client.saveRequests
        XCTAssertEqual(requests.count, 1)
    }

    func testWrongRetryKeyConflictKeepsOriginalReceiptAndAllowsCorrectReentry() async {
        let client = FakeProviderSettingsClient(connections: [], scenario: .init(loseSaveResponse: true))
        let store = ProviderSettingsStore(client: client)
        await store.load()
        store.editor.displayName = "Exactly once"
        store.editor.apiKey = key
        await store.save()
        let originalID = store.saveRecoverySummary?.requestID
        await store.retryPendingSave(apiKey: "different-fake-key")
        XCTAssertTrue(store.saveNeedsReconciliation)
        XCTAssertEqual(store.saveRecoverySummary?.requestID, originalID)
        XCTAssertTrue(store.canRetryPendingSave)
        await store.retryPendingSave(apiKey: key)
        XCTAssertFalse(store.saveNeedsReconciliation)
        let requests = await client.saveRequests
        let connections = await client.connections
        let mutations = await client.saveMutationCount
        XCTAssertEqual(requests.count, 3)
        XCTAssertEqual(Set(requests.map(\.requestID)).count, 1)
        XCTAssertEqual(connections.count, 1)
        XCTAssertEqual(mutations, 1)
    }

    func testKeychainFailureCanAdoptLatestPublicConnectionThenRepairSameID() async throws {
        for creating in [true, false] {
            let client = FakeProviderSettingsClient(
                connections: creating ? [] : [ProviderSettingsFixtures.connection()],
                scenario: .init(failKeychainAfterMetadata: true))
            let store = ProviderSettingsStore(client: client)
            await store.load()
            store.editor.displayName = "Metadata saved, key failed"
            store.editor.apiKey = key
            await store.save()
            XCTAssertTrue(store.saveNeedsReconciliation)
            XCTAssertFalse(store.canAdoptSavedConnectionForRecovery)
            await store.retryPendingSave(apiKey: key)
            XCTAssertTrue(store.saveNeedsReconciliation)
            let failedMutations = await client.saveMutationCount
            XCTAssertEqual(failedMutations, 1)
            await store.load(discardEdits: true)
            let latest = try XCTUnwrap(store.selectedConnection)
            XCTAssertFalse(latest.credentialPresent)
            XCTAssertTrue(store.canAdoptSavedConnectionForRecovery)
            store.adoptSavedConnectionAfterReview(connectionID: latest.connectionID, expectedRevision: latest.revision)
            XCTAssertFalse(store.saveNeedsReconciliation)
            XCTAssertEqual(store.editor.connection?.revision, latest.revision)
            XCTAssertEqual(store.editor.apiKey, "")
            await client.configure(.init())
            store.editor.apiKey = "explicit-repair-key"
            await store.save()
            let requests = await client.saveRequests
            let connections = await client.connections
            XCTAssertEqual(connections.count, 1)
            XCTAssertEqual(store.selectedConnection?.connectionID, latest.connectionID)
            XCTAssertEqual(store.selectedConnection?.revision, latest.revision + 1)
            XCTAssertTrue(store.selectedConnection?.credentialPresent ?? false)
            XCTAssertEqual(requests[0].requestID, requests[1].requestID)
            XCTAssertNotEqual(requests[1].requestID, requests[2].requestID)
            XCTAssertEqual(requests[2].connectionID, latest.connectionID)
            XCTAssertEqual(requests[2].expectedRevision, latest.revision)
        }
    }

    func testOldReplayReceiptUsesLatestRenamedSnapshotInsteadOfOldSettings() async throws {
        let client = FakeProviderSettingsClient(connections: [], scenario: .init(loseSaveResponse: true))
        let store = ProviderSettingsStore(client: client)
        await store.load()
        store.editor.displayName = "Original name"
        store.editor.apiKey = key
        await store.save()
        let savedConnections = await client.connections
        let saved = try XCTUnwrap(savedConnections.first)
        let renamed = ProviderSettingsFixtures.changed(saved, revision: saved.revision + 1, name: "Changed elsewhere", credential: false)
        await client.replaceConnections([renamed])
        await store.retryPendingSave(apiKey: key)
        XCTAssertFalse(store.saveNeedsReconciliation)
        XCTAssertEqual(store.editor.displayName, "Changed elsewhere")
        XCTAssertEqual(store.editor.connection?.revision, renamed.revision)
        XCTAssertFalse(store.editor.connection?.credentialPresent ?? true)
        let mutations = await client.saveMutationCount
        XCTAssertEqual(mutations, 1)
    }

    func testRenamedNewConnectionCanBeAdoptedAfterHumanReviewWithoutKeyReentry() async throws {
        let client = FakeProviderSettingsClient(connections: [], scenario: .init(loseSaveResponse: true))
        let store = ProviderSettingsStore(client: client)
        await store.load()
        store.editor.displayName = "Original name"
        store.editor.apiKey = key
        await store.save()
        let savedConnections = await client.connections
        let saved = try XCTUnwrap(savedConnections.first)
        let renamed = ProviderSettingsFixtures.changed(saved, revision: saved.revision + 1, name: "Renamed by another client")
        await client.replaceConnections([renamed])
        await store.load(discardEdits: true)
        XCTAssertTrue(store.saveNeedsReconciliation)
        XCTAssertTrue(store.canAdoptSavedConnectionForRecovery)
        store.adoptSavedConnectionAfterReview(connectionID: renamed.connectionID, expectedRevision: renamed.revision)
        XCTAssertFalse(store.saveNeedsReconciliation)
        XCTAssertEqual(store.editor.displayName, renamed.displayName)
        let requests = await client.saveRequests
        XCTAssertEqual(requests.count, 1)
    }

    func testAdoptionRejectsChangedReviewRevision() async throws {
        let client = FakeProviderSettingsClient(scenario: .init(failKeychainAfterMetadata: true))
        let store = ProviderSettingsStore(client: client)
        await store.load()
        store.editor.apiKey = key
        await store.save()
        await store.load(discardEdits: true)
        let current = try XCTUnwrap(store.selectedConnection)
        store.adoptSavedConnectionAfterReview(connectionID: current.connectionID, expectedRevision: current.revision - 1)
        XCTAssertTrue(store.saveNeedsReconciliation)
        XCTAssertFalse(store.canSave)
    }

    func testMissingUpdatedIDCanBeExplicitlyDiscardedWithoutCreatingConnection() async {
        let client = FakeProviderSettingsClient(scenario: .init(saveFailure: ProviderSettingsFailure(.transport)))
        let store = ProviderSettingsStore(client: client)
        await store.load()
        store.editor.displayName = "Pending update"
        await store.save()
        await client.replaceConnections([])
        await store.load(discardEdits: true)
        XCTAssertTrue(store.canDiscardMissingConnectionChange)
        XCTAssertFalse(store.canAddConnection)
        store.discardMissingConnectionChangeAfterReview()
        XCTAssertFalse(store.saveNeedsReconciliation)
        XCTAssertTrue(store.canAddConnection)
        let requests = await client.saveRequests
        XCTAssertEqual(requests.count, 1)
    }

    func testDeletedConnectionAfterSuccessfulReceiptDoesNotRestoreStaleConnection() async {
        let client = FakeProviderSettingsClient(connections: [], scenario: .init(loseSaveResponse: true))
        let store = ProviderSettingsStore(client: client)
        await store.load()
        store.editor.displayName = "Deleted later"
        store.editor.apiKey = key
        await store.save()
        await client.replaceConnections([])
        await store.retryPendingSave(apiKey: key)
        XCTAssertFalse(store.saveNeedsReconciliation)
        XCTAssertNil(store.selectedConnection)
        XCTAssertTrue(store.canAddConnection)
        XCTAssertTrue(store.notice?.contains("現在の一覧に存在しません") ?? false)
        let mutations = await client.saveMutationCount
        XCTAssertEqual(mutations, 1)
    }

    func testMatchingPublicFieldsFromAnotherClientCannotConfirmOriginalSave() async throws {
        for creating in [true, false] {
            let client = FakeProviderSettingsClient(
                connections: creating ? [] : [ProviderSettingsFixtures.connection()],
                scenario: .init(saveFailure: ProviderSettingsFailure(.transport)))
            let store = ProviderSettingsStore(client: client)
            await store.load()
            store.editor.displayName = "Matching public fields"
            store.editor.apiKey = key
            await store.save()
            let originalRequestID = try XCTUnwrap(store.saveRecoverySummary?.requestID)
            let otherWrite = ProviderSettingsFixtures.connection(
                revision: creating ? 1 : 2, name: "Matching public fields", credential: true)
            await client.replaceConnections([otherWrite])
            await store.load(discardEdits: true)
            XCTAssertTrue(store.saveNeedsReconciliation)
            XCTAssertEqual(store.saveRecoverySummary?.requestID, originalRequestID)
            XCTAssertFalse(store.saveRecoverySummary?.receiptConfirmed ?? true)
            XCTAssertFalse(store.canSave)
            XCTAssertFalse(store.canAddConnection)
            XCTAssertTrue(store.canAdoptSavedConnectionForRecovery)
            await store.save()
            store.newConnection()
            let requests = await client.saveRequests
            XCTAssertEqual(requests.count, 1)
        }
    }

    func testDismissAndReopenKeepAmbiguousCreateRequestWithoutRetainingKey() async throws {
        for delivered in [true, false] {
            let client = FakeProviderSettingsClient(
                connections: [], scenario: delivered
                    ? .init(loseSaveResponse: true) : .init(saveFailure: ProviderSettingsFailure(.transport)))
            let store = ProviderSettingsStore(client: client)
            await store.load()
            store.editor.displayName = "Reopened pending save"
            store.editor.apiKey = key
            await store.save()
            let originalRequestID = try XCTUnwrap(store.saveRecoverySummary?.requestID)
            store.dismiss()
            XCTAssertFalse(store.isLoaded)
            XCTAssertFalse(store.canAddConnection)
            XCTAssertFalse(store.canRetryPendingSave)
            XCTAssertEqual(store.editor.apiKey, "")
            XCTAssertFalse(String(reflecting: store.pendingSave).contains(key))
            await store.load()
            XCTAssertTrue(store.saveNeedsReconciliation)
            XCTAssertEqual(store.saveRecoverySummary?.requestID, originalRequestID)
            XCTAssertFalse(store.canAddConnection)
            XCTAssertTrue(store.canRetryPendingSave)
            await client.configure(.init())
            await store.retryPendingSave(apiKey: key)
            XCTAssertFalse(store.saveNeedsReconciliation)
            let requests = await client.saveRequests
            let connections = await client.connections
            let mutations = await client.saveMutationCount
            XCTAssertEqual(requests.count, 2)
            XCTAssertEqual(requests[0], requests[1])
            XCTAssertEqual(connections.count, 1)
            XCTAssertEqual(mutations, 1)
        }
    }
}
