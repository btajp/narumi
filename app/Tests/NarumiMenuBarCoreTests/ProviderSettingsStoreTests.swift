import Foundation
import XCTest
@testable import NarumiMenuBarCore

@MainActor
final class ProviderSettingsStoreTests: XCTestCase {
    func testInitialLoadUsesOnlyLocalSnapshots() async {
        let client = FakeProviderSettingsClient()
        let store = ProviderSettingsStore(client: client)
        XCTAssertFalse(store.canSave)
        await store.load()
        XCTAssertTrue(store.isLoaded)
        XCTAssertEqual(store.editor.apiKey, "")
        XCTAssertEqual(store.selectedConnectionID, ProviderSettingsFixtures.connectionID)
        let auth = await client.authRequests
        let setup = await client.setupRequests
        let models = await client.modelRequests
        let tests = await client.testRequests
        XCTAssertTrue(auth.isEmpty)
        XCTAssertTrue(setup.isEmpty)
        XCTAssertTrue(models.isEmpty)
        XCTAssertTrue(tests.isEmpty)
    }

    func testSavingClearsInputAndDismissPreservesRequestWithoutApplyingLateCompletion() async throws {
        let client = FakeProviderSettingsClient(scenario: .init(holdSave: true))
        let store = ProviderSettingsStore(client: client)
        await store.load()
        store.editor.displayName = "Edited connection"
        store.editor.apiKey = "not-a-real-key"
        let saving = Task { await store.save() }
        await client.waitForSaveStart()
        XCTAssertEqual(store.editor.apiKey, "")
        XCTAssertTrue(store.isBusy)
        XCTAssertFalse(store.canSave)
        let originalRequestID = try XCTUnwrap(store.saveRecoverySummary?.requestID)
        store.dismiss()
        await client.releaseSave()
        await saving.value
        XCTAssertEqual(store.editor.apiKey, "")
        XCTAssertEqual(store.editor.connection?.revision, 1)
        XCTAssertEqual(store.editor.displayName, "Test connection")
        XCTAssertFalse(store.isLoaded)
        XCTAssertNil(store.notice)
        XCTAssertNil(store.errorMessage)
        XCTAssertEqual(store.saveRecoverySummary?.requestID, originalRequestID)
        await store.load()
        XCTAssertTrue(store.saveNeedsReconciliation)
        XCTAssertFalse(store.canSave)
        await store.retryPendingSave(apiKey: "not-a-real-key")
        XCTAssertFalse(store.saveNeedsReconciliation)
        let requests = await client.saveRequests
        let mutations = await client.saveMutationCount
        XCTAssertEqual(requests.count, 2)
        XCTAssertEqual(requests[0], requests[1])
        XCTAssertEqual(mutations, 1)
    }

    func testFailedSaveHidesUpstreamMessageAndNeverRestoresSecret() async {
        let client = FakeProviderSettingsClient(scenario: .init(saveRawFailure: true))
        let store = ProviderSettingsStore(client: client)
        await store.load()
        store.editor.apiKey = "not-a-real-key"
        await store.save()
        XCTAssertEqual(store.editor.apiKey, "")
        XCTAssertFalse(store.errorMessage?.contains("not-a-real-key") ?? true)
        XCTAssertFalse(store.errorMessage?.contains("upstream echo") ?? true)
        XCTAssertTrue(store.errorMessage?.contains("入力は消去") ?? false)
        XCTAssertEqual(store.selectedConnection?.revision, 1)
        let saves = await client.saveRequests
        XCTAssertEqual(saves.count, 1)
    }

    func testEnableAndExplicitKeyClearAreSavedWithoutAuthenticationOrGeneration() async {
        let client = FakeProviderSettingsClient()
        let store = ProviderSettingsStore(client: client)
        await store.load()
        store.editor.enabled = false
        store.editor.setClearAPIKey(true)
        await store.save()
        XCTAssertFalse(store.selectedConnection?.enabled ?? true)
        XCTAssertFalse(store.selectedConnection?.credentialPresent ?? true)
        XCTAssertEqual(store.selectedConnection?.lastGenerationState, .never)
        XCTAssertEqual(store.editor.apiKey, "")
        let saves = await client.saveRequests
        let auth = await client.authRequests
        XCTAssertEqual(saves.first?.apiKey, .clear)
        XCTAssertEqual(saves.first?.enabled, false)
        XCTAssertTrue(auth.isEmpty)
    }

    func testMetadataConnectionSuccessDoesNotClaimGenerationSuccess() async {
        let client = FakeProviderSettingsClient()
        let store = ProviderSettingsStore(client: client)
        await store.load()
        await store.testConnection()
        XCTAssertEqual(store.lastTest?.connected, true)
        XCTAssertEqual(store.selectedConnection?.authState, .authenticated)
        XCTAssertEqual(store.selectedConnection?.lastGenerationState, .never)
        XCTAssertEqual(store.selectedConnection?.revision, 1)
        XCTAssertTrue(store.notice?.contains("生成は未検証") ?? false)
        let tests = await client.testRequests
        XCTAssertEqual(tests.count, 1)
    }

    func testSavedAndRefreshedCandidatesNeverApplyAModelOrMutateConnection() async {
        let client = FakeProviderSettingsClient()
        let store = ProviderSettingsStore(client: client)
        await store.load()
        await store.loadModels()
        await store.loadModels(refresh: true)
        XCTAssertEqual(store.models.first?.availability, .unverified)
        XCTAssertNil(store.models.first?.billing.inputUSDPerMillionTokens)
        XCTAssertEqual(store.editor.connection?.revision, 1)
        let modelRequests = await client.modelRequests
        let saves = await client.saveRequests
        let auth = await client.authRequests
        XCTAssertEqual(modelRequests.map(\.refresh), [false, true])
        XCTAssertTrue(modelRequests.allSatisfy { $0.role == .llm })
        XCTAssertTrue(saves.isEmpty)
        XCTAssertTrue(auth.isEmpty)
    }

    func testDisabledConnectionCanReadCacheButCannotContactProvider() async {
        let client = FakeProviderSettingsClient(connections: [ProviderSettingsFixtures.connection(enabled: false)])
        let store = ProviderSettingsStore(client: client)
        await store.load()
        XCTAssertFalse(store.canTest)
        await store.testConnection()
        await store.startAuthentication()
        await store.loadModels(refresh: true)
        await store.loadModels()
        let tests = await client.testRequests
        let auth = await client.authRequests
        let models = await client.modelRequests
        XCTAssertTrue(tests.isEmpty)
        XCTAssertTrue(auth.isEmpty)
        XCTAssertEqual(models.map(\.refresh), [false])
    }

    func testStaleCatalogRevisionBlocksUseWithoutShowingWrongCandidates() async {
        let client = FakeProviderSettingsClient(scenario: .init(modelRevision: 99))
        let store = ProviderSettingsStore(client: client)
        await store.load()
        await store.loadModels()
        XCTAssertTrue(store.models.isEmpty)
        XCTAssertTrue(store.revisionConflict)
        XCTAssertFalse(store.canSave)
        XCTAssertTrue(store.errorMessage?.contains("configuration_conflict") ?? false)
    }

    func testRevisionConflictPreservesDraftUntilExplicitReload() async {
        let client = FakeProviderSettingsClient()
        let store = ProviderSettingsStore(client: client)
        await store.load()
        store.editor.displayName = "Local draft"
        await client.replaceConnections([ProviderSettingsFixtures.connection(revision: 2, name: "Other client edit")])
        await store.load()
        XCTAssertTrue(store.revisionConflict)
        XCTAssertEqual(store.editor.displayName, "Local draft")
        XCTAssertFalse(store.canSave)
        await store.load(discardEdits: true)
        XCTAssertFalse(store.revisionConflict)
        XCTAssertEqual(store.editor.displayName, "Other client edit")
        XCTAssertEqual(store.editor.connection?.revision, 2)
    }

    func testLostAuthenticationResponseQueriesOriginalStartWithoutStartingAgain() async throws {
        let client = FakeProviderSettingsClient(scenario: .init(loseAuthResponse: true))
        let store = ProviderSettingsStore(client: client)
        await store.load()
        await store.startAuthentication()
        await store.refreshOperations()
        let starts = await client.authRequests
        let lookups = await client.authLookups
        XCTAssertEqual(starts.count, 1)
        XCTAssertEqual(lookups.count, 1)
        XCTAssertEqual(lookups[0].startRequestID, try XCTUnwrap(starts.first).requestID)
        XCTAssertNil(lookups[0].operationID)
        XCTAssertEqual(store.pendingAuthentication?.state, .succeeded)
        XCTAssertEqual(store.selectedConnection?.authState, .authenticated)
        XCTAssertEqual(store.selectedConnection?.lastGenerationState, .never)
    }

    func testUnknownAuthenticationCannotBeImplicitlyRetried() async {
        let client = FakeProviderSettingsClient(scenario: .init(loseAuthResponse: true, authLookupState: .unknown))
        let store = ProviderSettingsStore(client: client)
        await store.load()
        await store.startAuthentication()
        XCTAssertEqual(store.pendingAuthentication?.state, .unknown)
        XCTAssertFalse(store.canTest)
        XCTAssertFalse(store.needsPolling)
        await store.startAuthentication()
        await store.refreshOperations()
        let starts = await client.authRequests
        let lookups = await client.authLookups
        XCTAssertEqual(starts.count, 1)
        XCTAssertEqual(lookups.count, 2)
        XCTAssertEqual(Set(lookups.compactMap(\.startRequestID)).count, 1)
    }

    func testOpeningSettingsRecoversActiveAuthenticationWithoutNewStart() async {
        let client = FakeProviderSettingsClient(connections: [
            ProviderSettingsFixtures.connection(activeAuth: ProviderSettingsFixtures.activeAuth())
        ])
        let store = ProviderSettingsStore(client: client)
        await store.load()
        XCTAssertTrue(store.needsPolling)
        XCTAssertEqual(store.pendingAuthentication?.startRequestID, "original-start")
        await store.refreshOperations()
        let starts = await client.authRequests
        let lookups = await client.authLookups
        XCTAssertTrue(starts.isEmpty)
        XCTAssertEqual(lookups.first?.startRequestID, "original-start")
        XCTAssertEqual(store.pendingAuthentication?.state, .succeeded)
    }

    func testPendingLogoutDoesNotReportCredentialAlreadyDeleted() async {
        let client = FakeProviderSettingsClient()
        let store = ProviderSettingsStore(client: client)
        await store.load()
        await store.logout()
        XCTAssertEqual(store.pendingAuthentication?.state, .pending)
        XCTAssertTrue(store.selectedConnection?.credentialPresent ?? false)
        XCTAssertFalse(store.notice?.contains("認証情報を削除しました") ?? true)
        await store.refreshOperations()
        XCTAssertFalse(store.selectedConnection?.credentialPresent ?? true)
    }

    func testLostSetupResponseRecoversFinishedLastSetupWithoutPreparingTwice() async throws {
        let client = FakeProviderSettingsClient(scenario: .init(loseSetupResponse: true, setupCompletesBeforeResponse: true))
        let store = ProviderSettingsStore(client: client)
        await store.load()
        await store.prepare(ProviderSettingsFixtures.resource(), providerID: .anthropicAPI)
        await store.refreshOperations()
        let requests = await client.setupRequests
        let recovered = try XCTUnwrap(store.recovery.setups[.anthropicAPI])
        XCTAssertEqual(requests.count, 1)
        XCTAssertEqual(recovered.startRequestID, requests[0].requestID)
        XCTAssertEqual(recovered.jobID, ProviderSettingsFixtures.jobID)
        XCTAssertEqual(recovered.state, .succeeded)
        XCTAssertEqual(store.selectedConnection?.lastGenerationState, .never)
    }

    func testRuntimeJobCanBeRecoveredAndCancelledWithoutNewSetup() async {
        let setup = ProviderSettingsFixtures.setup()
        let client = FakeProviderSettingsClient(providers: [ProviderSettingsFixtures.provider(state: .preparing, activeSetup: setup)])
        let store = ProviderSettingsStore(client: client)
        await store.load()
        XCTAssertEqual(store.recovery.setups[.anthropicAPI]?.jobID, setup.jobID)
        await store.refreshOperations()
        await store.cancelSetup(providerID: .anthropicAPI)
        let starts = await client.setupRequests
        let cancelled = await client.cancelledJobs
        XCTAssertTrue(starts.isEmpty)
        XCTAssertEqual(cancelled, [setup.jobID])
        XCTAssertEqual(store.recovery.setups[.anthropicAPI]?.state, .cancelled)
    }
}
