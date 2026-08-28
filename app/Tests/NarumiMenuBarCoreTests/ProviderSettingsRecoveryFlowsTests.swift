import Foundation
import XCTest
@testable import NarumiMenuBarCore

@MainActor
final class ProviderSettingsRecoveryFlowsTests: XCTestCase {
    func testAuthenticationRejectedBeforeAcceptanceDoesNotCreatePermanentUnknownWork() async {
        for code in [ProviderSettingsErrorCode.configurationConflict, .busy, .invalidArgument, .notFound, .authenticationRequired] {
            let client = FakeProviderSettingsClient(scenario: .init(authRejection: ProviderSettingsFailure(code)))
            let store = ProviderSettingsStore(client: client)
            await store.load()
            await store.startAuthentication()
            XCTAssertNil(store.pendingAuthentication, code.rawValue)
            let lookups = await client.authLookups
            XCTAssertTrue(lookups.isEmpty, code.rawValue)
            await store.load(discardEdits: true)
            XCTAssertTrue(store.canTest, code.rawValue)
            store.editor.displayName = "Can edit again"
            XCTAssertTrue(store.canSave, code.rawValue)
        }
    }

    func testRuntimeRejectedBeforeAcceptanceCanRecoverAfterCatalogReload() async {
        let client = FakeProviderSettingsClient(scenario: .init(setupRejection: ProviderSettingsFailure(.configurationConflict)))
        let store = ProviderSettingsStore(client: client)
        await store.load()
        await store.prepare(ProviderSettingsFixtures.resource(), providerID: .anthropicAPI)
        XCTAssertNil(store.recovery.setups[.anthropicAPI])
        XCTAssertFalse(store.canPrepare(ProviderSettingsFixtures.resource(), provider: store.providers[0]))
        await store.load(discardEdits: true)
        XCTAssertTrue(store.canPrepare(ProviderSettingsFixtures.resource(), provider: store.providers[0]))
        let requests = await client.setupRequests
        XCTAssertEqual(requests.count, 1)
    }

    func testAmbiguousEngineFailureKeepsSetupReceiptUnresolved() async {
        let client = FakeProviderSettingsClient(scenario: .init(setupRejection: ProviderSettingsFailure(.engineUnavailable)))
        let store = ProviderSettingsStore(client: client)
        await store.load()
        await store.prepare(ProviderSettingsFixtures.resource(), providerID: .anthropicAPI)
        XCTAssertEqual(store.recovery.setups[.anthropicAPI]?.state, .unknown)
        XCTAssertFalse(store.canPrepare(ProviderSettingsFixtures.resource(), provider: store.providers[0]))
    }

    func testLostCreateResponseRequiresExplicitSnapshotReviewInsteadOfAnotherUUID() async throws {
        let client = FakeProviderSettingsClient(connections: [], scenario: .init(loseSaveResponse: true))
        let store = ProviderSettingsStore(client: client)
        await store.load()
        store.editor.displayName = "Created once"
        store.editor.apiKey = "not-a-real-key"
        await store.save()
        XCTAssertEqual(store.editor.apiKey, "")
        XCTAssertTrue(store.saveNeedsReconciliation)
        XCTAssertFalse(store.canSave)
        XCTAssertFalse(store.canAddConnection)
        await store.save()
        store.newConnection()
        await store.load(discardEdits: true)
        XCTAssertTrue(store.saveNeedsReconciliation)
        XCTAssertFalse(store.canAddConnection)
        let reviewed = try XCTUnwrap(store.selectedConnection)
        store.adoptSavedConnectionAfterReview(connectionID: reviewed.connectionID, expectedRevision: reviewed.revision)
        XCTAssertFalse(store.saveNeedsReconciliation)
        XCTAssertFalse(store.editor.isCreating)
        XCTAssertEqual(store.selectedConnection?.displayName, "Created once")
        XCTAssertTrue(store.selectedConnection?.credentialPresent ?? false)
        XCTAssertEqual(store.editor.apiKey, "")
        let requests = await client.saveRequests
        let connections = await client.connections
        XCTAssertEqual(requests.count, 1)
        XCTAssertEqual(connections.count, 1)
        XCTAssertEqual(store.selectedConnectionID, try XCTUnwrap(connections.first).connectionID)
    }

    func testEmptySnapshotDoesNotTreatAmbiguousCreateAsNeverAccepted() async {
        let client = FakeProviderSettingsClient(connections: [], scenario: .init(saveFailure: ProviderSettingsFailure(.transport)))
        let store = ProviderSettingsStore(client: client)
        await store.load()
        store.editor.displayName = "Unknown create"
        await store.save()
        await store.load(discardEdits: true)
        XCTAssertTrue(store.saveNeedsReconciliation)
        XCTAssertFalse(store.canAddConnection)
        XCTAssertFalse(store.canSave)
        let requests = await client.saveRequests
        XCTAssertEqual(requests.count, 1)
    }

    func testDirtyDraftSurvivesSameRevisionAuthenticationCompletionAndCanSave() async {
        let client = FakeProviderSettingsClient(connections: [
            ProviderSettingsFixtures.connection(activeAuth: ProviderSettingsFixtures.activeAuth())
        ])
        let store = ProviderSettingsStore(client: client)
        await store.load()
        store.editor.displayName = "Unsaved draft"
        XCTAssertFalse(store.canSave)
        await store.refreshOperations()
        XCTAssertEqual(store.pendingAuthentication?.state, .succeeded)
        XCTAssertNil(store.selectedConnection?.activeAuth)
        XCTAssertNil(store.editor.connection?.activeAuth)
        XCTAssertEqual(store.editor.displayName, "Unsaved draft")
        XCTAssertTrue(store.canSave)
    }

    func testExternalDeletionCannotMoveOldModelAndConnectionSuccessToAnotherConnection() async {
        let client = FakeProviderSettingsClient()
        let store = ProviderSettingsStore(client: client)
        await store.load()
        await store.loadModels()
        await store.testConnection()
        XCTAssertFalse(store.models.isEmpty)
        XCTAssertNotNil(store.lastTest)
        await client.replaceConnections([ProviderSettingsFixtures.connection(connectionID: "conn-abcdef012345", name: "Another connection")])
        await store.load()
        XCTAssertEqual(store.selectedConnectionID, "conn-abcdef012345")
        XCTAssertTrue(store.models.isEmpty)
        XCTAssertNil(store.lastTest)
    }

    func testNewRuntimeSetupDoesNotDisplayPreviousFailedJobAfterFastCompletion() async {
        let client = FakeProviderSettingsClient(scenario: .init(setupCompletesBeforeResponse: true))
        let store = ProviderSettingsStore(client: client)
        await store.load()
        var oldJob = ProviderSettingsFixtures.job(state: "failed")
        oldJob.jobID = "job-abcdef012345"
        store.setupJobs[.anthropicAPI] = oldJob
        await store.prepare(ProviderSettingsFixtures.resource(), providerID: .anthropicAPI)
        XCTAssertEqual(store.recovery.setups[.anthropicAPI]?.state, .succeeded)
        XCTAssertNil(store.setupJobs[.anthropicAPI])
    }
}
